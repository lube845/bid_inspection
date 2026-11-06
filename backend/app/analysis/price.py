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
from app.utils.text import compact_snippet

_TOTAL_PATTERN = re.compile(
    r"(?:投标总报价|投标报价|总报价|报价总额|投标总价|总价|投标价格|报价金额)"
    r"\s*(?:为|是)?\s*[:：]?\s*(?:人民币)?\s*"
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*"
    r"(亿元|百万元|万元|千元|万|亿|元)",
)
# 只有大写金额的写法：投标总报价：壹仟万元整
_UPPER_TOTAL_PATTERN = re.compile(
    r"(?:投标总报价|投标报价|总报价|报价总额|投标总价|总价|投标价格|报价金额)"
    r"\s*(?:为|是)?\s*[:：]?\s*(?:人民币)?\s*"
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
}


@dataclass
class Quote:
    company_id: str
    company_name: str
    amount: float
    raw: str
    upper: str | None
    block: BlockRef
    start: int
    end: int

    @property
    def source(self) -> dict:
        return self.block.source(self.start, self.end)


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


def extract_quote(company_id: str, company_name: str, blocks: list[BlockRef]) -> Quote | None:
    """提取投标总报价。

    文档里可能同时出现分项报价表、总报价表和各种说明文字，所以不能取第一个
    匹配项：按「关键词权重 + 是否有大写金额」挑最像总报价的那一条。
    """
    candidates: list[tuple[int, int, Quote]] = []
    for order, block in enumerate(blocks):
        upper_match = _UPPER_PATTERN.search(block.content)
        upper_text = upper_match.group(1) if upper_match else None
        has_upper = 1 if upper_match else 0

        for match in _TOTAL_PATTERN.finditer(block.content):
            keyword = match.group(0)
            amount_text = match.group(1).replace(",", "")
            try:
                amount = float(amount_text) * _UNIT_MULTIPLIER[match.group(2)]
            except (ValueError, KeyError):
                continue
            if amount <= 0:
                continue
            score = _keyword_weight(keyword) * 10 + has_upper * 5
            quote = Quote(
                company_id=company_id,
                company_name=company_name,
                amount=amount,
                raw=match.group(0),
                upper=upper_text,
                block=block,
                start=match.start(1),
                end=match.end(2),
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
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


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

    special = [q for q in quotes if _special_tail(q.amount)]
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

    tails: dict[str, list[Quote]] = {}
    for quote in quotes:
        tails.setdefault(str(int(quote.amount))[-3:].zfill(3), []).append(quote)
    for tail, grouped in tails.items():
        if len(grouped) >= 3 and tail not in _SPECIAL_TAILS:
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


def _special_tail(amount: float) -> bool:
    return str(int(amount))[-3:].zfill(3) in _SPECIAL_TAILS


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
                }
            )

        label = RULE_LABELS[finding.rule]
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
                    "note": "价格异常为项目级识别结果，需结合报价文件和业务口径复核",
                },
                source_a=finding.quotes[0].source if finding.quotes else None,
                source_b=finding.quotes[1].source if len(finding.quotes) > 1 else None,
            )
        )
    return drafts


def quote_summary(quotes: list[Quote]) -> list[dict]:
    if not quotes:
        return []
    mean = statistics.mean(q.amount for q in quotes)
    return [
        {
            "company_id": q.company_id,
            "company_name": q.company_name,
            "amount": round(q.amount, 2),
            "amount_upper": q.upper or amount_to_upper(q.amount),
            "deviation_rate": round((q.amount - mean) / mean, 5) if mean else 0.0,
            "file_name": q.block.file_name,
            "page_index": q.block.page_index,
            "snippet": compact_snippet(q.block.content, 200),
        }
        for q in quotes
    ]
