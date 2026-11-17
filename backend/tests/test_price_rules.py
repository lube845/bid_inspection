"""价格规则：尾数参数真正生效、单家大小写一致性与质量项不计入组合风险。

此前 ``special_tail`` / ``tail_concentration_min_count`` 只存在规则版本里，
引擎用的是硬编码常量；这组测试保证参数修改真的会改变结果。
"""

from __future__ import annotations

from app.analysis.price import (
    QUALITY_RULES,
    Quote,
    analyze_prices,
    analyze_quote_quality,
    build_price_evidences,
    parse_upper_amount,
)
from app.analysis.sources import BlockRef


def _block(content: str = "投标总报价：1,000,000.00 元") -> BlockRef:
    return BlockRef(
        company_id="c1",
        company_name="甲公司",
        file_id="f1",
        file_name="标书.pdf",
        file_type="pdf",
        file_version=1,
        page_index=1,
        printed_page_number=None,
        block_id="b1",
        block_index=0,
        content=content,
        normalized=content,
        block_type="paragraph",
    )


def _quote(amount: float, name: str = "甲公司", upper: str | None = None) -> Quote:
    return Quote(
        company_id=name,
        company_name=name,
        amount=amount,
        raw=f"投标总报价：{amount}",
        upper=upper,
        block=_block(),
        start=0,
        end=6,
    )


def _rules_of(findings) -> set[str]:
    return {finding.rule for finding in findings}


def test_special_tail_uses_rule_value():
    quotes = [_quote(1_000_111), _quote(2_000_222), _quote(3_000_333)]

    # 默认取值 888/999/998 不匹配 111/222/333
    assert "special_tail" not in _rules_of(analyze_prices(quotes))

    custom = analyze_prices(quotes, thresholds={"special_tail": ["111"]})
    assert "special_tail" in _rules_of(custom)
    drafts = build_price_evidences(custom, 2_000_000)
    assert any("111" in str(item.evidence_data["metrics"]) for item in drafts)


def test_tail_concentration_threshold_is_configurable():
    quotes = [_quote(1_001_555), _quote(2_002_555)]

    assert "tail_concentration" not in _rules_of(analyze_prices(quotes))
    assert "tail_concentration" in _rules_of(
        analyze_prices(quotes, thresholds={"tail_concentration_min_count": 2})
    )


def test_amount_case_mismatch_is_quality_only():
    # 数字是一百万，大写成两百万
    quotes = [_quote(1_000_000, "甲公司", upper="贰佰万元整")]
    findings = analyze_quote_quality(quotes)

    assert len(findings) == 1
    assert findings[0].rule in QUALITY_RULES
    drafts = build_price_evidences(findings, 1_000_000)
    assert drafts[0].evidence_data["quality_only"] is True


def test_amount_case_consistency_can_be_disabled():
    quotes = [_quote(1_000_000, "甲公司", upper="贰佰万元整")]
    findings = analyze_quote_quality(
        quotes, thresholds={"price_amount_case_consistency": False}
    )
    assert findings == []


def test_matching_case_amount_is_not_reported():
    amount = 9_986_000
    quotes = [_quote(amount, "甲公司", upper="玖佰玖拾捌万陆仟元整")]
    assert parse_upper_amount(quotes[0].upper) == amount
    assert analyze_quote_quality(quotes) == []
