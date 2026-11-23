"""错字 / 非规范写法的跨企业一致性.

文本相似度解决「长段雷同」，但默认最短匹配 40 字：一句错字加十几字上下文
一致，足以说明来自同一底稿，却不会成立证据。这正是现有链路的结构性盲区。

做法是确定性的、可解释的：

1. 词表（常见错别字 / 非规范写法 → 正确写法）来自规则版本，管理员可维护；
2. 在原文中扫出命中，记录错误词**前后各 N 字**的上下文；
3. 两家出现同一错误写法，且上下文规范化后完全相同 → 成立证据；
4. 该错误写法在本批次内出现企业数 ≥ 阈值时视为行业通病，只记录不抬风险。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.analysis.base import EvidenceDraft
from app.analysis.sources import BlockRef, BlockSpan
from app.models.enums import EvidenceType, RiskLevel
from app.utils.text import normalize

# 上下文窗口（规范化字符数）。按「由长到短」逐级匹配：窗口越长越可信，
# 但改写往往从紧邻文字开始，因此保留较短一档以兼顾召回。
CONTEXT_CHARS = 12
CONTEXT_LEVELS = (12, 8, 5)
@dataclass
class AnomalyOccurrence(BlockSpan):
    term: str
    correction: str
    context: str
    block: BlockRef
    start: int
    end: int
    # 命中词左右两侧的规范化上下文（各最多 CONTEXT_CHARS 字）
    left: str = ""
    right: str = ""


def _side_levels(a: AnomalyOccurrence, b: AnomalyOccurrence) -> tuple[int, int]:
    """分别返回「命中词之前」「命中词之后」一致的最大窗口长度。

    两侧独立比较：错误词出现在句首时左侧没有上下文，只有右侧一致也能成立，
    但要求另一侧确实足够长（见 ``build_anomaly_evidences`` 的总分门槛）。
    """
    left_avail = min(len(a.left), len(b.left), CONTEXT_CHARS)
    right_avail = min(len(a.right), len(b.right), CONTEXT_CHARS)
    left_level = max(
        (level for level in CONTEXT_LEVELS if level <= left_avail and a.left[-level:] == b.left[-level:]),
        default=0,
    )
    right_level = max(
        (level for level in CONTEXT_LEVELS if level <= right_avail and a.right[:level] == b.right[:level]),
        default=0,
    )
    return left_level, right_level


def _context_level(a: AnomalyOccurrence, b: AnomalyOccurrence) -> int:
    """上下文一致度：两侧窗口长度之和；任意一侧独自达到 8 字也成立。"""
    left_level, right_level = _side_levels(a, b)
    if max(left_level, right_level) < 5:
        return 0
    total = left_level + right_level
    return total if total >= 8 else 0


# 默认词表：只收录「确实是错误写法 → 正确写法」的常见项。
# 同音/形近但语义不同的词（如「按揭」「违约金」）不收录，避免把正常用词当错误。
DEFAULT_TYPO_TERMS = {
    "帐号": "账号",
    "帐户": "账户",
    "按装": "安装",
    "峻工": "竣工",
    "峻收": "验收",
    "法订代表人": "法定代表人",
    "投表": "投标",
    "招表": "招标",
    "按排": "安排",
    "检查院": "检察院",
    "侯选": "候选",
    "侯车": "候车",
    "验收合可": "验收合格",
    "质量保正": "质量保证",
    "进厂施工": "进场施工",
    "鉴订": "签订",
    "签定": "签订",
    "签属": "签署",
    "履约保正金": "履约保证金",
    "投标保正金": "投标保证金",
    "工成": "工程",
    "附属设使": "附属设施",
    "设使": "设施",
    "相附合": "相符合",
    "符和要求": "符合要求",
    "于期": "逾期",
    "交负": "交付",
    "验受": "验收",
    "部暑": "部署",
    "布暑": "部署",
    "幅射": "辐射",
    "既往不究": "既往不咎",
    "严紧违章": "严禁违章",
    "作费": "作废",
    "投拆": "投诉",
    "清淅": "清晰",
    "分辩率": "分辨率",
    "祥细": "详细",
    "祥情": "详情",
    "己完成": "已完成",
    "以经": "已经",
    "联营体": "联合体",
}


def _match_context(content: str, start: int, end: int) -> tuple[str, str]:
    """取命中词前后各 CONTEXT_CHARS 个规范化字符。

    原文可能含标点与空格，先多取一些原文再规范化，才能稳定拿到足够的字。
    """
    left = normalize(content[max(0, start - CONTEXT_CHARS * 4) : start])[-CONTEXT_CHARS:]
    right = normalize(content[end : end + CONTEXT_CHARS * 4])[:CONTEXT_CHARS]
    return left, right


def extract_anomalies(
    blocks: list[BlockRef], terms: dict[str, str] | None = None
) -> list[AnomalyOccurrence]:
    """在一家企业的文本文块中扫出非规范写法。"""
    dictionary = terms or DEFAULT_TYPO_TERMS
    if not dictionary:
        return []
    # 长词优先，避免「签定」先命中后把「签定合同」的整体语境切碎
    ordered = sorted(dictionary, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(term) for term in ordered))

    results: list[AnomalyOccurrence] = []
    seen: set[tuple[str, str]] = set()
    for block in blocks:
        for match in pattern.finditer(block.content):
            term = match.group(0)
            left, right = _match_context(block.content, match.start(), match.end())
            # 至少一侧要有 5 个字上下文，否则无法说明「同一处错误」而非巧合
            if len(left) < 5 and len(right) < 5:
                continue
            context = f"{left}|{right}"
            key = (term, block.company_id, context)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                AnomalyOccurrence(
                    term=term,
                    correction=dictionary[term],
                    context=context,
                    block=block,
                    start=match.start(),
                    end=match.end(),
                    left=left,
                    right=right,
                )
            )
    return results


def _batch_common_terms(
    occurrences_by_company: dict[str, list[AnomalyOccurrence]], min_companies: int
) -> set[str]:
    """本批次里多家共有的错误写法：行业通病，不抬风险。"""
    presence: dict[str, set[str]] = {}
    for company_id, occurrences in occurrences_by_company.items():
        for occurrence in occurrences:
            presence.setdefault(occurrence.term, set()).add(company_id)
    return {term for term, companies in presence.items() if len(companies) >= min_companies}


def build_anomaly_evidences(
    company_a: tuple[str, str],
    company_b: tuple[str, str],
    occurrences_a: list[AnomalyOccurrence],
    occurrences_b: list[AnomalyOccurrence],
    *,
    batch_common: set[str] | None = None,
) -> list[EvidenceDraft]:
    """两家出现同一错误写法且上下文一致时生成证据。"""
    common = batch_common or set()
    by_term_b: dict[str, list[AnomalyOccurrence]] = {}
    for item in occurrences_b:
        by_term_b.setdefault(item.term, []).append(item)

    grouped: dict[str, dict] = {}
    for item_a in occurrences_a:
        best: tuple[int, AnomalyOccurrence] | None = None
        for item_b in by_term_b.get(item_a.term, []):
            level = _context_level(item_a, item_b)
            if level and (best is None or level > best[0]):
                best = (level, item_b)
        if best is None:
            continue
        level, item_b = best
        entry = grouped.setdefault(
            item_a.term,
            {
                "term": item_a.term,
                "correction": item_a.correction,
                "context": item_a.context,
                "context_chars": level,
                "excluded": item_a.term in common,
                "a": [],
                "b": [],
            },
        )
        entry["context_chars"] = max(entry["context_chars"], level)
        entry["a"].append(item_a)
        entry["b"].append(item_b)

    drafts: list[EvidenceDraft] = []
    for entry in grouped.values():
        if not entry["a"] or not entry["b"]:
            continue
        first_a = entry["a"][0]
        first_b = entry["b"][0]
        excluded = entry["excluded"]
        drafts.append(
            EvidenceDraft(
                evidence_type=EvidenceType.TEXT.value,
                subtype="shared_anomaly",
                severity=RiskLevel.LOW.value if excluded else RiskLevel.MEDIUM.value,
                summary=(
                    f"{company_a[1]} 与 {company_b[1]} 在相同上下文中出现同一处非规范写法"
                    f"「{entry['term']}」（应为「{entry['correction']}」）"
                    + ("；该写法在本批次多家文件中出现，按行业通病处理" if excluded else "")
                ),
                evidence_data={
                    "fact": "anomaly",
                    "term": entry["term"],
                    "correction": entry["correction"],
                    "context": entry["context"],
                    "context_chars": entry["context_chars"],
                    "occurrences_a": len(entry["a"]),
                    "occurrences_b": len(entry["b"]),
                    "batch_common": excluded,
                    "excluded_by_template": excluded,
                    "note": (
                        "同一处非规范写法及其前后文字完全一致，通常意味着两份文件出自同一底稿；"
                        "需结合原件与编辑记录核实。"
                    ),
                },
                source_a=first_a.source,
                source_b=first_b.source,
            )
        )
    return drafts
