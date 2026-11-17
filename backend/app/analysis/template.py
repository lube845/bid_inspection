"""批次内共同模板识别（模板噪声抑制）.

为什么需要它：

    招标文件对减只能剔除「招标人写的公共内容」。但投标人之间大量雷同来自
    行业范本、通用承诺函、施工组织设计的套话——这些东西不来自本次招标文件，
    对减漏掉，就会被算成「两家企业文本雷同」。3 家以上项目里，同一段范本会
    在 N 家标书里同时出现，证据条数被整体抬高，风险等级失真。

判定口径（三条同时满足才判为共同模板）：

1. **批次内共现**：该文本块在 ``min_companies`` 家及以上企业的标书里都能
   找到近似副本（Jaccard ≥ ``similarity``）；
2. **低信息量**：数字、型号、英文与专有标识的密度低于 ``max_informativeness``。
   洗稿抄来的技术方案通常充满参数与型号，信息量高，不会被这一步误杀；
3. **不是长段逐字雷同**：命中长度超过 ``max_chars_guard`` 的一律保留为证据。
   几百字逐字相同本身就是强证据，不能因为「大家都这么写」就抹掉。

命中项**不删除**，只是打上与 ``excluded_by_tender`` 同级的
``excluded_by_template`` 标记：仍保留识别记录供复核，但不计入风险条数。
这样审核人能看到「这条被当作模板排除了」，而不是证据凭空消失。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.analysis.shingling import shingle_set

# 信息量的三个来源：数字、拉丁字母（型号/英文/公式）、单位与专有符号。
# 中文套话几乎不含这些，技术方案则密度很高。
_DIGIT_WEIGHT = 1.0
_LATIN_WEIGHT = 1.0
# 单位、公差、百分号等：出现在技术参数里的概率远高于套话
_SYMBOL_WEIGHT = 0.6
_SYMBOL_PATTERN = re.compile(
    r"[%‰±×÷≤≥<>~°]|(?:\d+(?:\.\d+)?)\s*(?:mw|kv|kw|mm|cm|m2|m3|kg|t|h|min|s)\b",
    re.I,
)


@dataclass
class TemplateProfile:
    """一次分析任务内的共同模板画像（只读，供各企业组合复用）。"""

    #: chunk_id -> 该文本块在批次内出现的企业数（含自身，至少 1）
    presence: dict[str, int] = field(default_factory=dict)
    #: chunk_id -> 信息量密度（0～1）
    informativeness: dict[str, float] = field(default_factory=dict)

    def is_template(
        self,
        chunk_id_a: str,
        chunk_id_b: str,
        matched_chars: int,
        *,
        min_companies: int,
        max_informativeness: float,
        max_chars_guard: int,
    ) -> bool:
        """判断一对命中是否属于「批次共同模板」。

        三条门槛都用 ````python`` 局部变量保持可读，便于以后在规则页把它
        拆成独立可配参数时逐条对应。
        """
        # 门槛 3：长段逐字雷同不当模板（宁可多报，不可漏掉铁证）
        if matched_chars >= max_chars_guard:
            return False

        # 门槛 1：两家企业都必须出现在至少 min_companies 家企业的共现集合里
        presence = min(
            self.presence.get(chunk_id_a, 1),
            self.presence.get(chunk_id_b, 1),
        )
        if presence < min_companies:
            return False

        # 门槛 2：两侧都是低信息量套话。
        # 这里取 max 而不是 min：只要有一侧是高信息量的实质内容（型号、参数、
        # 技术指标），这段雷同就可能是真线索，不能因为另一侧是套话而放过。
        info = max(
            self.informativeness.get(chunk_id_a, 1.0),
            self.informativeness.get(chunk_id_b, 1.0),
        )
        return info <= max_informativeness


def informativeness(text: str) -> float:
    """项目特异信息密度：数字、拉丁字母与单位符号占有效字符的比例。

    不做分词，避免引入词典依赖；对「套话 vs 参数」的区分已经足够：
    施工组织设计套话的数字占比通常 < 2%，含型号参数的技术方案 > 10%。
    """
    if not text:
        return 1.0
    total = 0
    weighted = 0.0
    for char in text:
        if char.isspace():
            continue
        total += 1
        if char.isdigit():
            weighted += _DIGIT_WEIGHT
        elif char.isascii() and char.isalpha():
            weighted += _LATIN_WEIGHT
    if not total:
        return 1.0
    weighted += _SYMBOL_WEIGHT * len(_SYMBOL_PATTERN.findall(text))
    # 密度理论上可以超过 1（符号重复计数），截断到 1 便于与阈值比较
    return min(1.0, weighted / total)


def build_template_profile(
    chunks_by_company: dict[str, list],
    *,
    similarity: float,
    shingle_size: int,
    index=None,
) -> TemplateProfile:
    """统计每个文本块在批次内的企业共现数，并计算信息量。

    ``index`` 传入项目级 TextIndex 时复用它的 LSH 索引做候选召回；
    未传时才对全部文本块做两两比较（仅用于单测与独立调用）。
    """
    profile = TemplateProfile()
    all_chunks = [chunk for chunks in chunks_by_company.values() for chunk in chunks]
    if not all_chunks:
        return profile

    shingle_cache: dict[int, set[int]] = {}

    def shingles_of(chunk) -> set[int]:
        key = id(chunk)
        if key not in shingle_cache:
            shingle_cache[key] = shingle_set(chunk.normalized, shingle_size)
        return shingle_cache[key]

    for chunk in all_chunks:
        profile.informativeness[chunk.chunk_id] = round(
            informativeness(chunk.normalized), 4
        )

    # 每个文本块记录「哪些其他企业有它的近似副本」
    matches: dict[str, set[str]] = {chunk.chunk_id: set() for chunk in all_chunks}

    def consider(chunk_a, chunk_b) -> None:
        if chunk_a.chunk_id == chunk_b.chunk_id:
            return
        # 同企业内部的重复不是「多家共现」，不计入
        if chunk_a.company_id == chunk_b.company_id:
            return
        if not _near_duplicate(chunk_a, chunk_b, shingles_of, similarity):
            return
        matches[chunk_a.chunk_id].add(chunk_b.company_id)
        matches[chunk_b.chunk_id].add(chunk_a.company_id)

    if index is not None:
        for chunk in all_chunks:
            signature = index.signatures.get(chunk.chunk_id)
            if signature is None:
                continue
            for other_id in index.lsh.query(signature, exclude=chunk.chunk_id):
                other = index.chunks.get(other_id)
                if other is not None:
                    consider(chunk, other)
    else:
        for i, chunk_a in enumerate(all_chunks):
            for chunk_b in all_chunks[i + 1 :]:
                consider(chunk_a, chunk_b)

    for chunk_id, companies in matches.items():
        profile.presence[chunk_id] = len(companies) + 1
    return profile


def _near_duplicate(chunk_a, chunk_b, shingles_of, similarity: float) -> bool:
    """用 Jaccard 判定两个文本块是否近似副本。"""
    set_a = shingles_of(chunk_a)
    set_b = shingles_of(chunk_b)
    if not set_a or not set_b:
        return False
    intersection = len(set_a & set_b)
    union = len(set_a) + len(set_b) - intersection
    return bool(union) and intersection / union >= similarity
