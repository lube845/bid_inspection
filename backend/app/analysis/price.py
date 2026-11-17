"""价格异常分析（V1 规则版）.

只做项目级规则识别，输出“异常模式 + 参与企业”，不把项目级异常硬拆成
单家企业结论。阈值来自配置，正式业务口径需业务方确认。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from app.analysis.base import EvidenceDraft
from app.analysis.sources import BlockRef
from app.analysis.thresholds import price_thresholds
from app.models.enums import EvidenceType, RiskLevel

# 总价标签。除完整的关键词外，还覆盖「投标总价（人民币）：」「投标报价金额」
# 等常见变体。注意「投标报价」「投标价格」也会出现在分项说明里，因此它们
# 只参与「标签在前」的常规匹配，不参与表格汇总行的低置信度匹配。
_TOTAL_KEYWORDS = (
    r"投标总报价(?:金额)?|投标报价(?:金额)?|投标总价(?:金额)?|总报价(?:金额)?"
    r"|报价总额|总价金额|投标价格|报价金额|总价|投标价|报价合计"
)
# 表格汇总行只认这些高置信度标签，防止「报价金额：120000元」这类分项小计
# 被当成总价（回归测试 test_total_quote_supports_yi_yuan_and_keeps_best_candidate）。
_TABLE_TOTAL_WORDS = (
    "投标总报价",
    "投标总价",
    "总报价",
    "报价总额",
    "投标价",
    "报价合计",
    "合计金额",
    "合计",
    "总计",
    "合价",
)


def _loose_word(word: str) -> str:
    """允许同名标签被空格/换行拆开：「合 计」「合\n计」（PDF 表格常见）。"""
    return r"\s*".join(word)


_TABLE_TOTAL_KEYWORDS = r"(?:" + "|".join(
    _loose_word(word) for word in sorted(_TABLE_TOTAL_WORDS, key=len, reverse=True)
) + r")"
_UNIT_PATTERN = r"亿元|百万元|万元|千元|万|亿|元"
# 数字：支持千分位、全角数字（NFKC 前的原文里可能出现）
_NUMBER_PATTERN = r"[0-9０-９][0-9０-９,，]*(?:\.[0-9０-９]{1,2})?"
# 标签与数字之间允许插入的填充：为、是、冒号、括号说明、货币符号、空白
_LABEL_TO_NUMBER = r"\s*(?:为|是)?\s*[:：]?\s*(?:人民币|RMB|￥|¥)?\s*"
# 兼容：
#   投标总报价：10,000,000.00 元
#   投标总报价（人民币元）：9986000
#   投标总报价（万元）：998.6
#   投标总报价：人民币 998.6 万元
#   投标总价（人民币）：1,400,000.00 元     ← 括号里是币种而不是单位
#   投标总报价（大写）：壹仟万元整            ← 纯大写金额走 _UPPER_TOTAL_PATTERN
_TOTAL_PATTERN = re.compile(
    rf"(?:{_TOTAL_KEYWORDS})"
    rf"(?:\s*[（(]\s*(?:人民币|大写)?\s*({_UNIT_PATTERN})\s*[）)])?"
    # 括号里只有币种、没有单位：投标总价（人民币）：1,400,000.00 元
    rf"(?:\s*[（(]\s*(?:人民币|RMB|￥|¥)\s*[）)])?"
    rf"{_LABEL_TO_NUMBER}"
    rf"({_NUMBER_PATTERN})\s*({_UNIT_PATTERN})?"
)
# 表格汇总行（标签在前）：兼容竖线/制表符分隔、"合计（元）"、标签与数字换行分隔
#   合计 | 1,400,000.00 元
#   合计（元）1,450,000.00
#   合计（元）\n1,450,000.00
_TABLE_TOTAL_PATTERN = re.compile(
    rf"(?:{_TABLE_TOTAL_KEYWORDS})"
    # 标签后的括号说明：元 / 元整 / 人民币 / 人民币元 / 金额
    rf"(?:\s*[（(]\s*(?:人民币)?\s*(?:金额)?\s*(?:{_UNIT_PATTERN})?\s*[）)])?"
    # 标签与数字之间允许换行（表格行解析后常被拆成上下两行），但不允许跨过
    # 任何其他文字——否则「备注/说明」行的数字会被误读成本行合计。
    rf"[ \t|｜:：]*\n?[ \t|｜:：]*"
    rf"(?:人民币|RMB|￥|¥)?[ \t]*({_NUMBER_PATTERN})[ \t]*({_UNIT_PATTERN})?"
)
# 表格汇总行（数字在前、标签在后）：
#   1,400,000.00 元（合计）
# 只接受「标签紧邻数字且在同一行」的写法。分项行「145,000.00\n合计（元）」
# 的数字属于上一行明细，绝不能当成本行合计——这正是实测错价的来源。
_TABLE_TOTAL_AFTER_PATTERN = re.compile(
    rf"([0-9０-９][0-9０-９,，]*(?:\.[0-9０-９]{{1,2}})?)[ \t]*({_UNIT_PATTERN})?"
    rf"[ \t]*[（(]\s*({_TABLE_TOTAL_KEYWORDS})\s*[）)]"
)
# 只有大写金额的写法：投标总报价：壹仟万元整
_UPPER_TOTAL_PATTERN = re.compile(
    rf"(?:{_TOTAL_KEYWORDS})"
    rf"(?:\s*[（(]\s*(?:人民币|大写)?\s*(?:{_UNIT_PATTERN})\s*[）)])?"
    # 括号里只有「人民币 / 大写」的写法：投标总报价（大写）：壹仟万元整
    rf"(?:\s*[（(]\s*(?:人民币|大写|RMB)\s*[）)])?"
    rf"{_LABEL_TO_NUMBER}"
    r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿兆圆元角分整]{3,})"
)
_UPPER_PATTERN = re.compile(
    r"(?:大写金额|金额大写|大写|人民币)\s*[:：]?\s*(?:人民币)?\s*"
    r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿兆圆元角分整]{3,})"
)

_UNIT_MULTIPLIER = {
    "亿元": 100_000_000,
    "百万元": 1_000_000,
    "万元": 10_000,
    "千元": 1_000,
    "万": 10_000,
    "亿": 100_000_000,
    "元": 1,
}

_SPECIAL_TAILS = {"888", "999", "998"}
# 常见取整尾数：整数万元 / 整数百元，作为单独信号噪声太大，默认不报
_COMMON_TAILS = {"000", "500"}


def _money(value: float) -> str:
    """安全格式化金额：None / 非数字都显示为「—」，避免 f-string 抛异常。"""
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"
# 尾数集中规则的最少家数；可由规则版本覆盖（见 price_thresholds）
_TAIL_MIN_COUNT = 3

# 关键词权重：越接近「投标总报价」越可能是总价，越靠后越可能是分项金额
_KEYWORD_WEIGHTS = {
    "投标总报价": 5,
    "投标总价": 5,
    "总报价": 5,
    "报价总额": 5,
    "投标报价": 3,
    "投标价格": 3,
    "总价": 1,
    "报价金额": 1,
}

_UPPER_DIGITS = {
    "零": 0,
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
}
_UPPER_SMALL_UNITS = {"拾": 10, "佰": 100, "仟": 1000}
_UPPER_BIG_UNITS = {"万": 10**4, "亿": 10**8, "兆": 10**12}


RULE_LABELS = {
    "high_concentration": "报价高度集中（变异系数过低）",
    "close_deviation": "多家报价偏差过小",
    "arithmetic_sequence": "报价呈等差分布",
    "special_tail": "特殊尾数",
    "tail_concentration": "尾数集中",
    "amount_case_mismatch": "大小写金额不一致",
    "quote_needs_confirmation": "自动识别金额待确认",
}

# 单家企业自身的质量项，不属于「多家串通」线索，不计入组合价格风险
QUALITY_RULES = {"amount_case_mismatch", "quote_needs_confirmation"}


@dataclass
class Quote:
    company_id: str
    company_name: str
    amount: float
    raw: str
    upper: str | None
    block: BlockRef | None
    start: int
    end: int
    # 人工导入/补录的报价没有原文片段，改用快照里的来源信息
    source_override: dict | None = None
    # manual | auto | import
    origin: str = "auto"
    quote_id: str | None = None
    quote_version_no: int | None = None
    # 同一文本块中「合计/总计」类标签命中的全部金额。多个值不一致说明表格
    # 结构没有按预期解析（例如把分项行当成了合计），需要人工确认。
    alternatives: list[float] | None = None
    # 抽取时已判定「候选金额互相矛盾」——快照还原时直接沿用，不重算。
    needs_confirmation: bool = False

    @property
    def inconsistent(self) -> bool:
        if self.needs_confirmation:
            return True
        values = {round(value, 2) for value in (self.alternatives or [])}
        values.add(round(self.amount, 2))
        return len(values) > 1

    @property
    def source(self) -> dict:
        if self.source_override is not None:
            return self.source_override
        if self.block is None:
            return {}
        return self.block.source(self.start, self.end)


def quote_from_snapshot(entry: dict) -> Quote | None:
    """把任务快照里的报价条目还原成 ``Quote``。

    快照条目由 ``analysis_service._ensure_price_snapshot`` 写入，来源可能是
    人工导入、手工补录或首次自动抽取——三者在这里统一，后续规则与证据构建
    完全不需要区分。
    """
    try:
        amount = float(entry["amount"])
    except (KeyError, TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return Quote(
        company_id=str(entry.get("company_id") or ""),
        company_name=str(entry.get("company_name") or ""),
        amount=amount,
        raw=str(entry.get("raw") or ""),
        upper=entry.get("amount_upper"),
        block=None,
        start=0,
        end=0,
        source_override=entry.get("source") or {
            "locator_precision": "quote",
            "source_kind": entry.get("source_kind") or "unknown",
        },
        origin=entry.get("source_kind") or "auto",
        quote_id=entry.get("quote_id"),
        quote_version_no=entry.get("quote_version_no"),
        needs_confirmation=bool(entry.get("needs_confirmation")),
    )


@dataclass
class PriceFinding:
    rule: str
    severity: str
    summary: str
    quotes: list[Quote]
    metrics: dict


def _parse_upper_integer(text: str) -> float | None:
    """解析中文大写金额的整数部分（壹佰贰拾叁万四千五百…）。"""
    if not text:
        return 0.0
    total = 0.0
    section = 0.0
    number = 0
    seen = False
    for char in text:
        if char in _UPPER_DIGITS:
            number = _UPPER_DIGITS[char]
            seen = True
        elif char in _UPPER_SMALL_UNITS:
            # 「拾佰仟」前面没有数字时按 1 处理（拾 = 十）
            section += (number or 1) * _UPPER_SMALL_UNITS[char]
            number = 0
            seen = True
        elif char in _UPPER_BIG_UNITS:
            section = (section + number) * _UPPER_BIG_UNITS[char]
            total += section
            section = 0.0
            number = 0
            seen = True
        elif char in "整正":
            continue
        else:
            return None
    if not seen:
        return None
    return total + section + number


def parse_upper_amount(text: str) -> float | None:
    """把人民币大写金额解析成数字，解析不了返回 None。

    兼容「壹佰伍拾万元整」「壹佰伍拾万圆整」「壹亿贰仟万整」（省略「元」）等写法。
    """
    if not text:
        return None
    cleaned = (
        text.replace(" ", "")
        .replace("　", "")
        .replace("人民币", "")
        .replace("RMB", "")
        .replace("rmb", "")
    )
    separator = re.search(r"[元圆]", cleaned)
    if separator:
        integer_part = cleaned[: separator.start()]
        fraction = cleaned[separator.end() :]
    else:
        # 「壹亿贰仟万整」这类写法省略了「元」
        integer_part = cleaned
        fraction = ""
    integer_part = integer_part.replace("整", "")
    integer = _parse_upper_integer(integer_part)
    if integer is None:
        return None
    amount = integer
    jiao = re.search(r"([零壹贰叁肆伍陆柒捌玖])角", fraction)
    if jiao:
        amount += _UPPER_DIGITS[jiao.group(1)] / 10
    fen = re.search(r"([零壹贰叁肆伍陆柒捌玖])分", fraction)
    if fen:
        amount += _UPPER_DIGITS[fen.group(1)] / 100
    # 金额精确到「分」，避免浮点误差在后续规则里放大
    return round(amount, 2)


def _keyword_weight(text: str) -> int:
    for keyword, weight in _KEYWORD_WEIGHTS.items():
        if keyword in text:
            return weight
    return 0


def _to_amount(number_text: str, unit: str | None) -> float | None:
    """把「1,400,000.00」「１４０００００」等写法的数字乘以单位换算成元。"""
    cleaned = (
        number_text.replace(",", "")
        .replace("，", "")
        .translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    )
    try:
        value = float(cleaned)
    except ValueError:
        return None
    multiplier = _UNIT_MULTIPLIER.get(unit or "元")
    if multiplier is None:
        return None
    amount = value * multiplier
    return amount if amount > 0 else None


def extract_quote(company_id: str, company_name: str, blocks: list[BlockRef]) -> Quote | None:
    """提取投标总报价。

    文档里可能同时出现分项报价表、总报价表和各种说明文字，所以不能取第一个
    匹配项：按「关键词权重 + 是否有大写金额」挑最像总报价的那一条。
    表格汇总行（合计 / 总计 / 数字在前）也纳入候选，但权重较低。
    """
    candidates: list[tuple[int, int, Quote]] = []
    for order, block in enumerate(blocks):
        upper_match = _UPPER_PATTERN.search(block.content)
        upper_text = upper_match.group(1) if upper_match else None
        has_upper = 1 if upper_match else 0

        # 先收集本块中所有「合计类」命中的金额：单个候选与多个候选不一致时，
        # 说明表格结构没有按预期解析，该块的所有候选都标记为待确认。
        table_amounts = [
            amount
            for match in _TABLE_TOTAL_PATTERN.finditer(block.content)
            for amount in [_to_amount(match.group(1), match.group(2) or "元")]
            if amount is not None
        ]
        table_amounts.extend(
            amount
            for match in _TABLE_TOTAL_AFTER_PATTERN.finditer(block.content)
            for amount in [_to_amount(match.group(1), match.group(2) or "元")]
            if amount is not None
        )

        for match in _TOTAL_PATTERN.finditer(block.content):
            keyword = match.group(0)
            # 单位可能写在括号里（人民币元）、数字后面（万元），也可能省略（按元）
            unit = match.group(3) or match.group(1) or "元"
            amount = _to_amount(match.group(2), unit)
            if amount is None:
                continue
            score = _keyword_weight(keyword) * 10 + has_upper * 5
            quote = Quote(
                company_id=company_id,
                company_name=company_name,
                amount=amount,
                raw=match.group(0),
                upper=upper_text,
                block=block,
                # group(1) 是可选括号单位，未参与匹配时 start(1) 为 -1，
                # 会被 source 钳到块首。改为从数字起点（group 2）定位。
                start=match.start(2) if match.start(2) >= 0 else match.start(0),
                end=match.end(2),
                alternatives=table_amounts or None,
            )
            candidates.append((score, -order, quote))

        # 表格汇总行：标签在前（合计 | 1,400,000.00 元）或数字在前（1,400,000.00 元（合计））。
        # 权重低于「投标总报价」等显式标签，只在没有更好候选时胜出。
        for match in _TABLE_TOTAL_PATTERN.finditer(block.content):
            amount = _to_amount(match.group(1), match.group(2) or "元")
            if amount is None:
                continue
            score = 4 * 10 + has_upper * 5
            quote = Quote(
                company_id=company_id,
                company_name=company_name,
                amount=amount,
                raw=match.group(0),
                upper=upper_text,
                block=block,
                start=match.start(1),
                end=match.end(1),
                alternatives=table_amounts,
            )
            candidates.append((score, -order, quote))

        for match in _TABLE_TOTAL_AFTER_PATTERN.finditer(block.content):
            amount = _to_amount(match.group(1), match.group(2) or "元")
            if amount is None:
                continue
            score = 4 * 10 + has_upper * 5
            quote = Quote(
                company_id=company_id,
                company_name=company_name,
                amount=amount,
                raw=match.group(0),
                upper=upper_text,
                block=block,
                start=match.start(1),
                end=match.end(1),
                alternatives=table_amounts,
            )
            candidates.append((score, -order, quote))

        # 只有大写金额的写法（投标总报价：壹仟万元整）
        for match in _UPPER_TOTAL_PATTERN.finditer(block.content):
            amount = parse_upper_amount(match.group(1))
            if not amount or amount <= 0:
                continue
            score = _keyword_weight(match.group(0)) * 10 + 5
            quote = Quote(
                company_id=company_id,
                company_name=company_name,
                amount=amount,
                raw=match.group(0),
                upper=match.group(1),
                block=block,
                start=match.start(1),
                end=match.end(1),
            )
            candidates.append((score, -order, quote))

    if not candidates:
        return None
    # 候选金额互相矛盾时优先选择「有显式总价标签」的那一条，同时保留待确认标记：
    # 表格结构与预期不符，结论不能静默采用。
    conflicting = any(quote.inconsistent for _, _, quote in candidates)
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = candidates[0][2]
    if conflicting:
        best.needs_confirmation = True
    return best


def amount_to_upper(amount: float) -> str:
    """把金额转换为人民币大写，用于展示/导出。"""
    digits = "零壹贰叁肆伍陆柒捌玖"
    units = ["", "拾", "佰", "仟"]
    big_units = ["", "万", "亿", "兆"]

    integer = int(amount)
    fraction = int(round((amount - integer) * 100))
    if fraction >= 100:
        integer += 1
        fraction = 0
    if integer == 0 and fraction == 0:
        return "零元整"

    groups: list[str] = []
    group_index = 0
    while integer > 0:
        group = integer % 10000
        integer //= 10000
        if group:
            text = ""
            zero = False
            for position in range(3, -1, -1):
                digit = (group // (10 ** position)) % 10
                if digit == 0:
                    zero = True
                    continue
                if zero and text:
                    text += "零"
                zero = False
                text += digits[digit] + units[position]
            groups.append(text + big_units[group_index])
        elif groups and not groups[-1].endswith("零"):
            groups.append("零")
        group_index += 1

    result = "".join(reversed(groups)).rstrip("零") or "零"
    if fraction == 0:
        return result + "元整"
    jiao, fen = divmod(fraction, 10)
    tail = (digits[jiao] + "角" if jiao else "") + (digits[fen] + "分" if fen else "")
    return result + "元" + tail


def analyze_prices(
    quotes: list[Quote], *, thresholds: dict | None = None
) -> list[PriceFinding]:
    rules = price_thresholds(thresholds)
    if len(quotes) < 2:
        return []

    amounts = [q.amount for q in quotes]
    mean = statistics.mean(amounts)
    findings: list[PriceFinding] = []
    # 尾数参数在读规则快照时就取好，供下面两条尾数规则共用
    tail_min = int(rules.get("tail_concentration_min_count") or _TAIL_MIN_COUNT)
    special_tails = set(rules.get("special_tail") or _SPECIAL_TAILS)

    deviations = [(q.amount - mean) / mean if mean else 0.0 for q in quotes]

    if len(quotes) >= 3 and mean > 0:
        cv = statistics.pstdev(amounts) / mean
        if cv < rules["price_cv"]:
            findings.append(
                PriceFinding(
                    rule="high_concentration",
                    severity=RiskLevel.HIGH.value,
                    summary=(
                        f"{len(quotes)} 家投标报价高度集中，变异系数 "
                        f"{cv * 100:.2f}%，低于 {rules['price_cv'] * 100:.0f}% 阈值"
                    ),
                    quotes=quotes,
                    metrics={"cv": round(cv, 5), "mean": round(mean, 2)},
                )
            )

    if len(quotes) >= 3:
        close = [
            q
            for q, deviation in zip(quotes, deviations)
            if abs(deviation) < rules["price_close_deviation"]
        ]
        if len(close) >= 3:
            findings.append(
                PriceFinding(
                    rule="close_deviation",
                    severity=RiskLevel.HIGH.value,
                    summary=(
                        f"{len(close)} 家投标报价与平均报价偏差小于 "
                        f"{rules['price_close_deviation'] * 100:.1f}%"
                    ),
                    quotes=close,
                    metrics={
                        "mean": round(mean, 2),
                        "deviations": [round(d * 100, 3) for d in deviations],
                    },
                )
            )

    sequence = _find_arithmetic_sequence(quotes, rules)
    if sequence:
        seq_quotes, step = sequence
        findings.append(
            PriceFinding(
                rule="arithmetic_sequence",
                severity=RiskLevel.HIGH.value,
                summary=f"存在 {len(seq_quotes)} 家报价呈等差数列，级差约 {step:,.2f} 元",
                quotes=seq_quotes,
                metrics={"step": round(step, 2)},
            )
        )

    special = [q for q in quotes if _special_tail(q.amount, special_tails)]
    if special:
        findings.append(
            PriceFinding(
                rule="special_tail",
                severity=RiskLevel.MEDIUM.value,
                summary=f"存在特殊尾数报价：{'、'.join(q.company_name for q in special)}",
                quotes=special,
                metrics={"tails": [str(int(q.amount))[-3:] for q in special]},
            )
        )

    # 尾数集中：整数万元（尾数 000）在采购报价里极其常见，本身不构成异常。
    # 这类取整尾数默认忽略；确实需要时可通过规则参数显式纳入（common_tails）。
    common_tails = {
        str(tail).zfill(3)
        for tail in (rules.get("common_tail_values") or _COMMON_TAILS)
    }
    tails: dict[str, list[Quote]] = {}
    for quote in quotes:
        tails.setdefault(str(int(quote.amount))[-3:].zfill(3), []).append(quote)
    for tail, grouped in tails.items():
        if tail in special_tails or tail in common_tails:
            continue
        if len(grouped) >= tail_min:
            findings.append(
                PriceFinding(
                    rule="tail_concentration",
                    severity=RiskLevel.MEDIUM.value,
                    summary=f"{len(grouped)} 家报价尾数集中为 {tail}",
                    quotes=grouped,
                    metrics={"tail": tail},
                )
            )

    return findings


def _find_arithmetic_sequence(
    quotes: list[Quote], rules: dict
) -> tuple[list[Quote], float] | None:
    ordered = sorted(quotes, key=lambda q: q.amount)
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            gap1 = ordered[j].amount - ordered[i].amount
            if gap1 <= 0:
                continue
            for k in range(j + 1, len(ordered)):
                gap2 = ordered[k].amount - ordered[j].amount
                if gap2 <= 0:
                    continue
                tolerance = rules["price_sequence_tolerance"] * max(abs(gap1), 1)
                if abs(gap1 - gap2) <= tolerance:
                    return [ordered[i], ordered[j], ordered[k]], (gap1 + gap2) / 2
    return None


def _special_tail(amount: float, special_tails: set[str] | None = None) -> bool:
    return str(int(amount))[-3:].zfill(3) in (special_tails or _SPECIAL_TAILS)


def analyze_quote_quality(
    quotes: list[Quote], *, thresholds: dict | None = None
) -> list[PriceFinding]:
    """单家报价的质量检查：数字金额与书写的大写金额是否一致.

    这类问题说明报价文件本身存在自相矛盾，需要人工核对，但它不是「多家串通」
    线索，因此 ``Rule`` 归入 ``QUALITY_RULES``，不计入组合的价格风险。
    """
    rules = price_thresholds(thresholds)
    if not rules.get("price_amount_case_consistency", True):
        return []
    findings: list[PriceFinding] = []
    for quote in quotes:
        if not quote.upper:
            continue
        parsed = parse_upper_amount(quote.upper)
        if parsed is None or abs(parsed - quote.amount) <= 0.01:
            continue
        findings.append(
            PriceFinding(
                rule="amount_case_mismatch",
                severity=RiskLevel.LOW.value,
                summary=(
                    f"{quote.company_name} 投标总报价书写为 {quote.amount:,.2f} 元，"
                    f"大写金额“{quote.upper}”解析为 {parsed:,.2f} 元，两者不一致"
                ),
                quotes=[quote],
                metrics={
                    "amount": round(quote.amount, 2),
                    "amount_upper_parsed": round(parsed, 2),
                    "amount_upper_raw": quote.upper,
                },
            )
        )
    # 自动识别金额待确认：表格里出现多个互相矛盾的合计候选。价格维度不能
    # 静默采用其中一个值，否则「覆盖 4/4」却报了错价。
    for quote in quotes:
        if not quote.inconsistent:
            continue
        alternatives = sorted(
            {round(value, 2) for value in (quote.alternatives or [])}
        )
        findings.append(
            PriceFinding(
                rule="quote_needs_confirmation",
                severity=RiskLevel.LOW.value,
                summary=(
                    f"{quote.company_name} 自动识别到多个合计金额（"
                    + "、".join(_money(value) for value in alternatives)
                    + f" 元），系统暂取 {_money(quote.amount)} 元，需人工确认"
                ),
                quotes=[quote],
                metrics={
                    "amount": round(quote.amount, 2),
                    "alternatives": alternatives,
                },
            )
        )
    return findings


def build_price_evidences(findings: list[PriceFinding], mean: float) -> list[EvidenceDraft]:
    drafts: list[EvidenceDraft] = []
    for finding in findings:
        quotes_payload = []
        for quote in finding.quotes:
            deviation = (quote.amount - mean) / mean if mean else 0.0
            quotes_payload.append(
                {
                    "company_id": quote.company_id,
                    "company_name": quote.company_name,
                    "amount": round(quote.amount, 2),
                    "amount_upper": quote.upper or amount_to_upper(quote.amount),
                    "deviation_rate": round(deviation, 5),
                    "raw": quote.raw,
                    "source": quote.source,
                    "origin": quote.origin,
                    "quote_id": quote.quote_id,
                    "quote_version_no": quote.quote_version_no,
                }
            )

        label = RULE_LABELS[finding.rule]
        is_quality = finding.rule in QUALITY_RULES
        drafts.append(
            EvidenceDraft(
                evidence_type=EvidenceType.PRICE.value,
                subtype=finding.rule,
                severity=finding.severity,
                summary=f"{label}：{finding.summary}",
                evidence_data={
                    "rule": finding.rule,
                    "rule_label": label,
                    "quotes": quotes_payload,
                    "metrics": finding.metrics,
                    "mean": round(mean, 2),
                    "participant_count": len(finding.quotes),
                    # 质量项不计入组合价格风险，前端与风险计算都读这个标记
                    "quality_only": is_quality,
                    "note": (
                        "单家报价文件本身存在矛盾，需人工核对；不参与组合价格风险计算。"
                        if is_quality
                        else "价格异常为项目级识别结果，需结合报价文件和业务口径复核"
                    ),
                },
                source_a=finding.quotes[0].source if finding.quotes else None,
                source_b=finding.quotes[1].source if len(finding.quotes) > 1 else None,
            )
        )
    return drafts
