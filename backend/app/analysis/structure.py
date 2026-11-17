"""结构指纹：章节树、表格骨架与图片顺序.

改写措辞可以规避字面查重，但「整章搬家」通常连结构一起搬：
标题顺序、编号体系、表格列结构、图片排列往往原样保留。

本维度与文本相似正交，输出普通 ``EvidenceDraft``（``METADATA`` +
``structure_similarity``），复用现有的存储、审核与导出链路。

最大的误报来源是**招标文件本身规定的章节结构**——所有投标人都按同一份
招标文件编排是最正常的事。因此比对前必须先建「招标标题基准」，命中它的
标题不计分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.analysis.base import EvidenceDraft
from app.analysis.chunking import _classify_line
from app.analysis.sources import BlockRef
from app.models.enums import EvidenceType, RiskLevel
from app.utils.text import normalize

# 表格行：Tab / 竖线 / 连续空格分隔，与 line_items 的行式清单同形
_TABLE_SPLIT = re.compile(r"[\t|]+|\s{2,}")
_PAGE_NOISE = re.compile(r"^(?:第\s*\d+\s*页.*|\d{1,4})$")

# 签字、盖章、落款这类短行会被识别成「标题」，但它们不是章节结构。
# 不排除会把「两家都有『投标人盖章』」算成结构相似，产生大量无意义命中。
_BOILERPLATE = {
    "投标人盖章", "投标人公章", "投标单位盖章", "盖章", "签字", "签章",
    "法定代表人签字", "法定代表人或授权代表签字", "授权代表签字",
    "日期", "年月日", "附", "附件", "备注", "说明", "注",
}


@dataclass
class StructureFingerprint:
    headings: list[str] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    images: list[str] = field(default_factory=list)


def _heading_sequence(blocks: list[BlockRef]) -> list[str]:
    """抽取标题序列（规范化后的文本）。

    与分块阶段一致地判断标题，但不复用 ``TextChunk.section``：分块时短块合并
    会把多个小节拼成「A；B」，无法还原真实层级。
    """
    result: list[str] = []
    lines: list[tuple[str, str | None]] = []
    for block in blocks:
        for raw in block.content.splitlines():
            stripped = raw.strip()
            if not stripped or _PAGE_NOISE.match(stripped):
                continue
            lines.append((stripped, block.block_type))
    for index, (text, block_type) in enumerate(lines):
        next_text = lines[index + 1][0] if index + 1 < len(lines) else None
        heading = _classify_line(text, next_text, block_type)
        if heading is None:
            continue
        normalized = normalize(heading.label)
        if not 2 <= len(normalized) <= 40:
            continue
        if normalized in _BOILERPLATE:
            continue
        result.append(normalized)
    return result


def _table_skeleton(blocks: list[BlockRef], *, min_cols: int = 3, min_rows: int = 3) -> list[dict]:
    """识别行式表格块，输出 (列数, 表头集合, 行数)。"""
    tables: list[dict] = []
    for block in blocks:
        rows = []
        for line in block.content.splitlines():
            cells = [cell.strip() for cell in _TABLE_SPLIT.split(line.strip()) if cell.strip()]
            if len(cells) >= min_cols:
                rows.append(cells)
        if len(rows) < min_rows:
            continue
        header = {normalize(cell) for cell in rows[0] if normalize(cell)}
        tables.append(
            {
                "columns": max(len(row) for row in rows),
                "header": header,
                "rows": len(rows),
            }
        )
    return tables


def build_structure_fingerprint(blocks: list[BlockRef]) -> StructureFingerprint:
    return StructureFingerprint(headings=_heading_sequence(blocks), tables=_table_skeleton(blocks))


def build_structure_fingerprint_with_images(
    blocks: list[BlockRef], images: list[dict] | None = None
) -> StructureFingerprint:
    fingerprint = build_structure_fingerprint(blocks)
    fingerprint.images = [item.get("sha256", "") for item in (images or []) if item.get("sha256")]
    return fingerprint


def _lcs(a: list[str], b: list[str]) -> list[str]:
    """最长公共子序列（标题数量通常 < 200，O(n*m) 足够）。"""
    if not a or not b:
        return []
    m, n = len(a), len(b)
    table = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            table[i][j] = (
                table[i + 1][j + 1] + 1
                if a[i] == b[j]
                else max(table[i + 1][j], table[i][j + 1])
            )
    result: list[str] = []
    i = j = 0
    while i < m and j < n:
        if a[i] == b[j]:
            result.append(a[i])
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return result


def _longest_common_run(a: list[str], b: list[str]) -> list[str]:
    """最长公共连续子序列（图片顺序必须是连续的才有意义）。"""
    best: list[str] = []
    previous: list[str] = []
    for item in a:
        current: list[str] = []
        for index, other in enumerate(b):
            if item == other:
                prev = previous[index - 1] if index else []
                current.append(prev[-1] + [item] if prev else [item])
            else:
                current.append([])
        for run in current:
            if len(run) > len(best):
                best = run
        previous = current
    return best


def compare_structures(
    a: StructureFingerprint,
    b: StructureFingerprint,
    *,
    tender_headings: set[str] | None = None,
    min_titles: int = 3,
    title_ratio: float = 0.5,
    min_tables: int = 3,
) -> dict:
    """比较两份结构指纹，返回各信号命中的详情。"""
    tender = tender_headings or set()

    def effective(headings: list[str]) -> list[str]:
        return [item for item in headings if item not in tender]

    headings_a = effective(a.headings)
    headings_b = effective(b.headings)
    common_titles = _lcs(headings_a, headings_b)
    unique_a = len(set(headings_a))
    unique_b = len(set(headings_b))

    title_hit = (
        len(common_titles) >= min_titles
        and unique_a > 0
        and unique_b > 0
        and len(common_titles) / max(1, min(unique_a, unique_b)) >= title_ratio
    )

    table_pairs = 0
    table_details: list[dict] = []
    used_b: set[int] = set()
    for table_a in a.tables:
        for index, table_b in enumerate(b.tables):
            if index in used_b:
                continue
            if table_a["columns"] != table_b["columns"]:
                continue
            if not table_a["header"] or not table_b["header"]:
                continue
            if table_a["header"] != table_b["header"]:
                continue
            rows_a, rows_b = table_a["rows"], table_b["rows"]
            if rows_a and rows_b and abs(rows_a - rows_b) / max(rows_a, rows_b) > 0.2:
                continue
            used_b.add(index)
            table_pairs += 1
            table_details.append(
                {
                    "columns": table_a["columns"],
                    "rows_a": rows_a,
                    "rows_b": rows_b,
                    "header": sorted(table_a["header"])[:6],
                }
            )
            break

    image_run = _longest_common_run(a.images, b.images) if a.images and b.images else []

    return {
        "title_common": len(common_titles),
        "title_total_a": unique_a,
        "title_total_b": unique_b,
        "title_hit": title_hit,
        "title_samples": common_titles[:8],
        "table_pairs": table_pairs,
        "table_hit": table_pairs >= min_tables,
        "table_details": table_details[:8],
        "image_run": len(image_run),
        "image_hit": len(image_run) >= 3,
    }


def build_structure_evidence(
    company_a: tuple[str, str],
    company_b: tuple[str, str],
    comparison: dict,
    *,
    source_a: dict | None = None,
    source_b: dict | None = None,
) -> EvidenceDraft | None:
    """结构相似度达到门槛时生成证据；否则返回 None。"""
    title_hit = comparison["title_hit"]
    table_hit = comparison["table_hit"]
    image_hit = comparison["image_hit"]

    # 仅标题一致不成立证据：投标文件按招标文件规定的格式编排是常态，
    # 章节名相同几乎没有区分度（真实数据里每对企业都会命中）。
    # 必须至少有一个「招标文件不会规定」的信号：表格骨架或图片顺序。
    if not (table_hit or image_hit):
        return None

    if title_hit and table_hit and image_hit:
        severity = RiskLevel.HIGH.value
    elif title_hit:
        severity = RiskLevel.MEDIUM.value
    else:
        severity = RiskLevel.LOW.value

    parts = []
    if title_hit:
        parts.append(f"{comparison['title_common']} 个章节标题按相同顺序一致")
    if table_hit:
        parts.append(f"{comparison['table_pairs']} 张表格的列数与表头一致")
    if image_hit:
        parts.append(f"{comparison['image_run']} 张图片按相同顺序出现")

    return EvidenceDraft(
        evidence_type=EvidenceType.METADATA.value,
        subtype="structure_similarity",
        severity=severity,
        summary=(
            f"{company_a[1]} 与 {company_b[1]} 的文档结构高度相似："
            + "；".join(parts)
            + "。章节结构按招标文件编排属正常，本结论主要依据表格骨架或图片顺序。"
        ),
        evidence_data={
            "fact": "structure",
            "comparison": comparison,
            "note": (
                "结构相似本身不构成结论，但它说明两份文件可能由同一份底稿派生："
                "改写措辞通常不会同时重建章节树、表格与图片顺序。"
            ),
        },
        source_a=source_a,
        source_b=source_b,
    )
