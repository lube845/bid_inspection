"""结构指纹与错字一致性。"""

from __future__ import annotations

from app.analysis.anomalies import (
    DEFAULT_TYPO_TERMS,
    _batch_common_terms,
    build_anomaly_evidences,
    extract_anomalies,
)
from app.analysis.sources import BlockRef
from app.analysis.structure import (
    build_structure_evidence,
    build_structure_fingerprint,
    compare_structures,
)


def _block(content: str, company_id="c1", name="甲公司", page=1) -> BlockRef:
    return BlockRef(
        company_id=company_id,
        company_name=name,
        file_id=f"f-{company_id}",
        file_name=f"{company_id}.pdf",
        file_type="pdf",
        file_version=1,
        page_index=page,
        printed_page_number=None,
        block_id=f"b-{company_id}-{page}-{hash(content) % 10000}",
        block_index=page,
        content=content,
        normalized=content,
        block_type="paragraph",
    )


_DOC_A = """第一章 项目实施方案
我方将严格按照招标文件要求组织实施。
第二章 质量管理措施
实行三级质量检查制度。
第二章 售后服务承诺
提供7×24小时响应。
第二章 安全管理措施
落实安全生产责任制。
"""
_DOC_B = """第一章 项目实施方案
按照招标文件的要求，我方会组织实施本项目。
第二章 质量管理措施
建立三级质量检查制度。
第二章 售后服务承诺
提供全天候响应服务。
第二章 安全管理措施
落实安全生产责任制。
"""

_TENDER = """第一章 项目实施方案
第二章 质量管理措施
第二章 售后服务承诺
第二章 安全管理措施
"""


def test_headings_are_extracted():
    fingerprint = build_structure_fingerprint([_block(_DOC_A)])
    assert "第一章项目实施方案" in fingerprint.headings
    assert len(fingerprint.headings) >= 4


def test_title_only_does_not_produce_evidence():
    """只有标题一致不成立证据：按招标文件格式编排是常态。"""
    fa = build_structure_fingerprint([_block(_DOC_A, "c1")])
    fb = build_structure_fingerprint([_block(_DOC_B, "c2", "乙公司")])
    comparison = compare_structures(fa, fb)
    assert comparison["title_hit"] is True
    assert build_structure_evidence(("c1", "甲公司"), ("c2", "乙公司"), comparison) is None


def test_title_plus_table_produces_evidence():
    table = "序号\t项目名称\t单位\t数量\t单价\n1\t土方开挖\tm3\t100\t50\n2\t混凝土浇筑\tm3\t80\t450\n3\t钢筋工程\tt\t20\t5000"
    fa = build_structure_fingerprint([_block(f"{_DOC_A}\n{table}", "c1")])
    fb = build_structure_fingerprint([_block(f"{_DOC_B}\n{table}", "c2", "乙公司")])
    comparison = compare_structures(fa, fb, min_tables=1)
    assert comparison["title_hit"] is True
    assert comparison["table_hit"] is True

    draft = build_structure_evidence(("c1", "甲公司"), ("c2", "乙公司"), comparison)
    assert draft is not None
    assert draft.subtype == "structure_similarity"
    assert draft.severity == "medium"


def test_tender_headings_are_excluded():
    table = "序号\t项目名称\t单位\t数量\t单价\n1\t土方开挖\tm3\t100\t50\n2\t混凝土浇筑\tm3\t80\t450\n3\t钢筋工程\tt\t20\t5000"
    fa = build_structure_fingerprint([_block(f"{_DOC_A}\n{table}", "c1")])
    fb = build_structure_fingerprint([_block(f"{_DOC_B}\n{table}", "c2", "乙公司")])
    tender = build_structure_fingerprint([_block(_TENDER, "tender", "招标文件")])
    comparison = compare_structures(fa, fb, tender_headings=set(tender.headings), min_tables=1)

    # 标题全部来自招标文件规定 → 标题信号失效，只剩表格信号（低风险）
    assert comparison["title_hit"] is False
    draft = build_structure_evidence(("c1", "甲"), ("c2", "乙"), comparison)
    assert draft is not None
    assert draft.severity == "low"


def test_table_skeleton_match():
    table_a = "序号\t项目名称\t单位\t数量\t单价\n1\t土方开挖\tm3\t100\t50\n2\t混凝土浇筑\tm3\t80\t450\n3\t钢筋工程\tt\t20\t5000"
    table_b = "序号\t项目名称\t单位\t数量\t单价\n1\t土方开挖\tm3\t120\t52\n2\t混凝土浇筑\tm3\t90\t460\n3\t钢筋工程\tt\t22\t5100"
    fa = build_structure_fingerprint([_block(table_a, "c1")])
    fb = build_structure_fingerprint([_block(table_b, "c2")])
    comparison = compare_structures(fa, fb, min_tables=1)
    assert comparison["table_pairs"] >= 1
    assert comparison["table_hit"] is True


def test_anomaly_shared_context_is_evidence():
    text_a = "本项目投标保正金为人民币五万元，按招标文件要求缴纳。"
    text_b = "我方投标保正金为人民币五万元，按要求办理。"
    occurrences_a = extract_anomalies([_block(text_a, "c1")])
    occurrences_b = extract_anomalies([_block(text_b, "c2")])
    assert occurrences_a and occurrences_b

    drafts = build_anomaly_evidences(
        ("c1", "甲公司"), ("c2", "乙公司"), occurrences_a, occurrences_b
    )
    assert len(drafts) == 1
    assert drafts[0].subtype == "shared_anomaly"
    assert drafts[0].evidence_data["term"] == "投标保正金"


def test_anomaly_different_context_is_not_evidence():
    text_a = "投标保正金金额为五万元整。"
    text_b = "本项目投标保正金将于开标后返还。"
    occurrences_a = extract_anomalies([_block(text_a, "c1")])
    occurrences_b = extract_anomalies([_block(text_b, "c2")])
    drafts = build_anomaly_evidences(
        ("c1", "甲公司"), ("c2", "乙公司"), occurrences_a, occurrences_b
    )
    assert drafts == []


def test_batch_common_anomaly_is_recorded_but_excluded():
    text_a = "投标保正金为五万元，按规定缴纳。"
    text_b = "投标保正金为五万元，按规定缴纳。"
    text_c = "投标保正金为五万元，按规定缴纳。"
    per_company = {
        "c1": extract_anomalies([_block(text_a, "c1")]),
        "c2": extract_anomalies([_block(text_b, "c2")]),
        "c3": extract_anomalies([_block(text_c, "c3")]),
    }
    common = _batch_common_terms(per_company, min_companies=3)
    assert "投标保正金" in common

    drafts = build_anomaly_evidences(
        ("c1", "甲"), ("c2", "乙"), per_company["c1"], per_company["c2"], batch_common=common
    )
    assert drafts[0].evidence_data["batch_common"] is True
    assert drafts[0].evidence_data["excluded_by_template"] is True


def test_dictionary_excludes_normal_words():
    # 词表只收录错误写法，正常用词不应命中
    assert "按揭" not in DEFAULT_TYPO_TERMS
    assert "违约金" not in DEFAULT_TYPO_TERMS
    assert extract_anomalies([_block("本项目无需按揭，供应商按期交付。")]) == []
