"""报价清单逐项比对（商务标 / 工程量清单）.

投标总报价只能回答「报了多少」；围标串标最有说服力的报价证据在**清单明细**里：

- 两家企业的逐项单价大片相同（甚至保留同样的两位小数），而市场价格本不该如此集中；
- 两家在不同分项上出现**同一个算术错误**（错，且错得一样）——这几乎只可能来自
  同一份底稿。

数据来源：DOCX 与 PDF 解析出的文本块。表格已被解析器转成「一行一条、单元格用
制表符分隔」的形式，这里按行识别「项目名称 → 单价」的配对，用项目名称做跨企业
对齐键（规范化后匹配，允许少量差异）。

限制（写进证据说明，避免被当成定论）：只处理能稳定识别的行式清单；嵌套合并单元格、
纯图片清单、`工程量清单` 的专业计价文件（GBQ 等）不在覆盖范围内。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.analysis.base import EvidenceDraft
from app.analysis.sources import BlockRef
from app.models.enums import EvidenceType, RiskLevel
from app.utils.text import normalize

# 行内单价识别：形如「1  土方开挖  1200  12.50」或「土方开挖\t12.50元」
# 只认小数点后 1～4 位的小数，避免把序号、数量、总价当成单价。
_PRICE_TOKEN = re.compile(r"(?:^|[\t\s|])(\d{1,9}(?:\.\d{1,4}))(?=$|[\t\s|元])")
_ROW_SPLIT = re.compile(r"[\t|]+|\s{2,}")
# 这些词出现在行里说明是表头或合计行，不做单品比价
_HEADER_WORDS = ("序号", "项目名称", "单位", "数量", "单价", "合价", "合计", "小计", "备注")


@dataclass
class LineItem:
    """清单里的一行：项目名称 + 单价。"""

    company_id: str
    company_name: str
    key: str  # 规范化后的项目名称，用作跨企业对齐键
    raw_name: str
    unit_price: float
    block: BlockRef
    start: int
    end: int

    @property
    def source(self) -> dict:
        return self.block.source(self.start, self.end)


@dataclass
class ItemComparison:
    """两家企业在同一项目上的单价对照。"""

    key: str
    name: str
    item_a: LineItem
    item_b: LineItem
    same_price: bool
    relative_diff: float


@dataclass
class LineItemReport:
    comparisons: list[ItemComparison] = field(default_factory=list)

    @property
    def shared_count(self) -> int:
        return len(self.comparisons)

    @property
    def identical_count(self) -> int:
        return sum(1 for item in self.comparisons if item.same_price)

    @property
    def identical_ratio(self) -> float:
        return self.identical_count / self.shared_count if self.shared_count else 0.0


def _strip_header_noise(cells: list[str]) -> list[str]:
    return [cell for cell in cells if cell and not any(word in cell for word in _HEADER_WORDS)]


def _row_cells(line: str) -> list[str]:
    return [cell.strip() for cell in _ROW_SPLIT.split(line) if cell.strip()]


def _parse_row(line: str, company_id: str, company_name: str, block: BlockRef) -> LineItem | None:
    """从一行里拆出「项目名称 + 单价」。

    规则：行内至少有一个小数形式的数字，取**最后一个**小数作为单价
    （清单列序通常是 名称 / 单位 / 数量 / 单价 / 合价，单价之后可能还有合价，
    所以优先取倒数第二个若其也是小数，否则取最后一个）。名称为单价左侧、
    去掉纯数字单元格后的最长中文片段。
    """
    if "\t" not in line and "  " not in line:
        return None
    cells = _strip_header_noise(_row_cells(line))
    if len(cells) < 2:
        return None

    price_positions = [
        (index, float(match.group(1)))
        for index, cell in enumerate(cells)
        for match in [_PRICE_TOKEN.fullmatch(f" {cell}")]
        if match
    ]
    if not price_positions:
        return None

    # 优先取「倒数第二个小数」：倒数第一个往往是合价
    position, unit_price = price_positions[-2] if len(price_positions) >= 2 else price_positions[-1]
    if unit_price <= 0:
        return None

    name_cells = [
        cell
        for index, cell in enumerate(cells)
        if index < position and re.search(r"[一-鿿]{2,}", cell)
    ]
    if not name_cells:
        return None
    raw_name = max(name_cells, key=len)
    key = normalize(raw_name)
    # 名称太短（如「其他」）无法可靠对齐，跳过
    if len(key) < 2:
        return None

    start = block.content.find(raw_name)
    if start < 0:
        start = 0
    return LineItem(
        company_id=company_id,
        company_name=company_name,
        key=key,
        raw_name=raw_name,
        unit_price=unit_price,
        block=block,
        start=start,
        end=start + len(raw_name),
    )


def extract_line_items(blocks: list[BlockRef]) -> dict[str, LineItem]:
    """抽取一家企业的清单行，同一项目名称保留第一条。"""
    items: dict[str, LineItem] = {}
    for block in blocks:
        for line in block.content.splitlines():
            item = _parse_row(line, block.company_id, block.company_name, block)
            if item and item.key not in items:
                items[item.key] = item
    return items


def compare_line_items(
    items_a: dict[str, LineItem],
    items_b: dict[str, LineItem],
    *,
    tolerance: float = 0.0001,
) -> LineItemReport:
    """按项目名称对齐两家企业的单价，统计完全相同/近乎相同的条数。"""
    report = LineItemReport()
    for key in items_a.keys() & items_b.keys():
        item_a, item_b = items_a[key], items_b[key]
        if item_a.unit_price <= 0 or item_b.unit_price <= 0:
            continue
        diff = abs(item_a.unit_price - item_b.unit_price) / max(
            abs(item_a.unit_price), abs(item_b.unit_price)
        )
        report.comparisons.append(
            ItemComparison(
                key=key,
                name=item_a.raw_name,
                item_a=item_a,
                item_b=item_b,
                same_price=diff <= tolerance,
                relative_diff=round(diff, 6),
            )
        )
    report.comparisons.sort(key=lambda item: (not item.same_price, item.name))
    return report


def build_line_item_evidence(
    report: LineItemReport,
    *,
    min_shared: int,
    min_identical: int,
    min_ratio: float,
) -> EvidenceDraft | None:
    """逐项单价雷同达到门槛时生成一条证据。

    门槛三项同时满足才算异常：足够多的共同分项（避免只有 1-2 项的小表）、
    足够多的完全相同条数、足够高的相同比例。只比「比例」会被只有两三行的
    小表刷成 100%，所以条数是必须的。
    """
    if report.shared_count < min_shared or report.identical_count < min_identical:
        return None
    if report.identical_ratio < min_ratio:
        return None

    identical = [item for item in report.comparisons if item.same_price]
    names = "、".join(item.name for item in identical[:5])
    more = " 等" if len(identical) > 5 else ""
    severity = (
        RiskLevel.HIGH.value
        if report.identical_ratio >= 0.8 and report.identical_count >= min_identical + 2
        else RiskLevel.MEDIUM.value
    )
    first = identical[0]
    return EvidenceDraft(
        evidence_type=EvidenceType.PRICE.value,
        subtype="line_item_duplicate",
        severity=severity,
        summary=(
            f"{first.item_a.company_name} 与 {first.item_b.company_name} 的报价清单中，"
            f"{report.shared_count} 个共同分项里有 {report.identical_count} 项单价完全相同"
            f"（{report.identical_ratio:.0%}）：{names}{more}"
        ),
        evidence_data={
            "rule": "line_item_duplicate",
            "rule_label": RULE_LABEL,
            "shared_count": report.shared_count,
            "identical_count": report.identical_count,
            "identical_ratio": round(report.identical_ratio, 4),
            "items": [
                {
                    "name": item.name,
                    "same_price": item.same_price,
                    "unit_price_a": item.item_a.unit_price,
                    "unit_price_b": item.item_b.unit_price,
                    "relative_diff": item.relative_diff,
                    "source_a": item.item_a.source,
                    "source_b": item.item_b.source,
                }
                for item in report.comparisons[:50]
            ],
            "note": (
                "逐项单价由正文/表格行自动识别，只覆盖可稳定解析的行式清单；"
                "单价相同也可能源于同一套市场定额，需结合清单来源与工程量核实。"
            ),
        },
        source_a=first.item_a.source,
        source_b=first.item_b.source,
    )


RULE_LABEL = "逐项单价雷同"
