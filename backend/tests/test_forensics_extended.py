"""B 组改进的回归测试：表格解析、逐项单价、图片哈希、rsid、事实冲突。"""

import tempfile
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.analysis import image_hash, rsid
from app.analysis.facts import build_fact_conflict_evidences, extract_facts
from app.analysis.forensics import build_image_evidence, build_rsid_evidence
from app.analysis.line_items import (
    build_line_item_evidence,
    compare_line_items,
    extract_line_items,
)
from app.analysis.sources import BlockRef
from app.services.parser import parse_document


def _block(content: str, company_id: str = "c1", company_name: str = "甲公司") -> BlockRef:
    return BlockRef(
        company_id=company_id,
        company_name=company_name,
        file_id="f1",
        file_name="标书.docx",
        file_type="docx",
        file_version=1,
        page_index=1,
        printed_page_number=None,
        block_id="b1",
        block_index=0,
        content=content,
        normalized=content,
        block_type="paragraph",
    )


# ------------------------------------------------------------ B1 DOCX 表格


def _make_docx(tmp_path: Path, rows: list[list[str]], closing: str = "") -> Path:
    from docx import Document

    document = Document()
    document.add_paragraph("商务部分")
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            table.cell(i, j).text = value
    if closing:
        document.add_paragraph(closing)
    path = tmp_path / "bid.docx"
    document.save(str(path))
    return path


def test_docx_tables_are_parsed_in_document_order(tmp_path):
    rows = [
        ["序号", "项目名称", "单位", "数量", "单价", "合价"],
        ["1", "土方开挖", "m3", "100", "12.50", "1250.00"],
    ]
    path = _make_docx(tmp_path, rows, closing="投标总报价：9,986,000.00 元")
    parsed = parse_document(path, "docx")
    text = parsed.pages[0].page_text

    # 表格内容必须进入正文，之前的解析器会整表丢失
    assert "土方开挖" in text
    assert "12.50" in text
    # 段落顺序：标题 -> 表格 -> 落款
    assert text.index("商务部分") < text.index("土方开挖") < text.index("投标总报价")
    assert "已提取 1 个表格" in parsed.pages[0].note


def test_docx_without_tables_still_parses(tmp_path):
    from docx import Document

    document = Document()
    document.add_paragraph("只有正文，没有表格")
    path = tmp_path / "plain.docx"
    document.save(str(path))

    parsed = parse_document(path, "docx")
    assert "只有正文" in parsed.pages[0].page_text
    assert "表格" not in parsed.pages[0].note


# --------------------------------------------------------- B1 逐项单价


LINE_ITEM_TEXT = "\n".join(
    [
        "序号\t项目名称\t单位\t数量\t单价\t合价",
        "1\t土方开挖\tm3\t100\t12.50\t1250.00",
        "2\t混凝土浇筑\tm3\t50\t380.00\t19000.00",
        "3\t钢筋制安\tt\t20\t4200.00\t84000.00",
        "4\t模板工程\tm2\t300\t45.00\t13500.00",
        "5\t砌体工程\tm3\t80\t260.00\t20800.00",
        "6\t屋面防水\tm2\t150\t88.00\t13200.00",
    ]
)


def test_line_items_are_extracted_from_table_rows():
    items = extract_line_items([_block(LINE_ITEM_TEXT)])
    assert "土方开挖" in items
    assert items["土方开挖"].unit_price == 12.5
    assert items["钢筋制安"].unit_price == 4200.0
    # 表头行不应被当成清单项
    assert "项目名称" not in items


def test_identical_unit_prices_produce_evidence():
    block_a = _block(LINE_ITEM_TEXT, "c1", "甲公司")
    block_b = _block(LINE_ITEM_TEXT, "c2", "乙公司")
    report = compare_line_items(
        extract_line_items([block_a]), extract_line_items([block_b])
    )
    assert report.shared_count == 6
    assert report.identical_count == 6
    assert report.identical_ratio == 1.0

    draft = build_line_item_evidence(
        report, min_shared=5, min_identical=4, min_ratio=0.6
    )
    assert draft is not None
    assert draft.subtype == "line_item_duplicate"
    assert draft.severity == "high"
    assert draft.evidence_data["identical_count"] == 6


def test_different_unit_prices_are_not_evidence():
    other = LINE_ITEM_TEXT.replace("12.50", "18.90").replace("380.00", "410.00")
    report = compare_line_items(
        extract_line_items([_block(LINE_ITEM_TEXT)]),
        extract_line_items([_block(other, "c2", "乙公司")]),
    )
    assert report.shared_count == 6
    assert report.identical_count == 4  # 只改了 2 项
    # 比例为 4/6=67%，条数达标，仍会触发——验证门槛同时生效
    assert build_line_item_evidence(
        report, min_shared=5, min_identical=5, min_ratio=0.9
    ) is None


def test_small_tables_do_not_trigger_on_ratio_alone():
    """只有两三行的小表即使 100% 相同也不应报（防止比例被小样本刷满）。"""
    small = "\n".join(
        [
            "序号\t项目名称\t单价\t合价",
            "1\t土方开挖\t12.50\t1250.00",
            "2\t混凝土浇筑\t380.00\t19000.00",
        ]
    )
    report = compare_line_items(
        extract_line_items([_block(small)]),
        extract_line_items([_block(small, "c2", "乙公司")]),
    )
    assert report.identical_ratio == 1.0
    assert build_line_item_evidence(
        report, min_shared=5, min_identical=4, min_ratio=0.6
    ) is None


# ------------------------------------------------------- B2 图片感知哈希


def _png_bytes(kind: str = "gradient", size: int = 64) -> bytes:
    """生成有结构的测试图：纯色图会被低信息量保护跳过。"""
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    if kind == "gradient":
        for x in range(size):
            shade = int(255 * x / max(size - 1, 1))
            draw.line([(x, 0), (x, size)], fill=(shade, shade, shade))
    elif kind == "circle":
        draw.ellipse([8, 8, size - 8, size - 8], fill=(200, 30, 30))
    elif kind == "stripes":
        for y in range(0, size, 8):
            draw.rectangle([0, y, size, y + 3], fill=(20, 20, 20))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_identical_image_bytes_have_same_fingerprint():
    pytest.importorskip("PIL")
    raw = _png_bytes("circle")
    first = image_hash.fingerprint_bytes(raw)
    second = image_hash.fingerprint_bytes(raw)
    assert first is not None and second is not None
    assert first.sha256 == second.sha256
    assert first.dhash == second.dhash


def test_different_images_are_not_matched():
    """渐变与条纹的 dHash 都会退化成全 0，必须靠 aHash 一起区分开。"""
    pytest.importorskip("PIL")
    left = image_hash.fingerprint_bytes(_png_bytes("gradient"))
    right = image_hash.fingerprint_bytes(_png_bytes("stripes"))
    assert left is not None and right is not None
    assert image_hash.compare_image_sets([left.to_dict()], [right.to_dict()]) == []


def test_blank_images_are_skipped_to_avoid_false_positives():
    """纯色/空白图的感知哈希彼此相同，必须直接跳过。"""
    pytest.importorskip("PIL")
    from io import BytesIO

    from PIL import Image

    for color in [(255, 255, 255), (0, 0, 0), (30, 120, 200)]:
        buffer = BytesIO()
        Image.new("RGB", (64, 64), color).save(buffer, format="PNG")
        assert image_hash.fingerprint_bytes(buffer.getvalue()) is None


def test_image_sets_match_identical_and_near_duplicates():
    pytest.importorskip("PIL")
    fingerprint = image_hash.fingerprint_bytes(_png_bytes("circle"))
    other = image_hash.fingerprint_bytes(_png_bytes("stripes"))
    assert fingerprint and other

    matches = image_hash.compare_image_sets(
        [fingerprint.to_dict()], [fingerprint.to_dict()]
    )
    assert len(matches) == 1 and matches[0]["identical"] is True

    # 完全不同的图不应配对
    assert image_hash.compare_image_sets(
        [fingerprint.to_dict()], [other.to_dict()]
    ) == []


def test_image_evidence_severity_depends_on_identical():
    file_a = {"file_id": "f1", "file_name": "甲.pdf"}
    file_b = {"file_id": "f2", "file_name": "乙.pdf"}
    identical = build_image_evidence(
        ("c1", "甲"), ("c2", "乙"), file_a, file_b, [{"identical": True}]
    )
    similar = build_image_evidence(
        ("c1", "甲"), ("c2", "乙"), file_a, file_b, [{"identical": False}]
    )
    assert identical.severity == "high"
    assert similar.severity == "medium"


# ------------------------------------------------------------- B2 rsid


def test_rsid_profiles_are_extracted_and_compared(tmp_path):
    from docx import Document

    document = Document()
    document.add_paragraph("正文")
    path = tmp_path / "a.docx"
    document.save(str(path))

    profile = rsid.extract_rsid(path)
    # python-docx 生成的文档带 rsid，没有则说明提取路径失效
    assert profile is not None
    assert profile.all_ids

    comparison = rsid.compare_rsid(profile, profile)
    assert comparison["shared_count"] == len(profile.all_ids)
    assert comparison["overlap_ratio"] == 1.0


def test_rsid_evidence_requires_threshold():
    file_a = {"file_id": "f1", "file_name": "甲.docx"}
    file_b = {"file_id": "f2", "file_name": "乙.docx"}
    assert build_rsid_evidence(
        ("c1", "甲"),
        ("c2", "乙"),
        file_a,
        file_b,
        {"shared_count": 2, "shared_trace_count": 2},
        min_shared=3,
    ) is None
    draft = build_rsid_evidence(
        ("c1", "甲"),
        ("c2", "乙"),
        file_a,
        file_b,
        {"shared_count": 8, "shared_trace_count": 6},
        min_shared=3,
    )
    assert draft is not None
    assert draft.severity == "high"


def test_rsid_missing_in_non_docx(tmp_path):
    path = tmp_path / "x.pdf"
    path.write_bytes(b"%PDF-1.4 not a real pdf")
    assert rsid.extract_rsid(path) is None


# ------------------------------------------------------- B5 关键事实


def test_shared_phone_number_is_high_risk():
    facts_a = extract_facts([_block("授权代表：王强 手机：13800138000", "c1", "甲")])
    facts_b = extract_facts([_block("联系人：王强 电话：13800138000", "c2", "乙")])
    drafts = build_fact_conflict_evidences(facts_a, facts_b, ("c1", "甲"), ("c2", "乙"))
    phone = [d for d in drafts if d.subtype == "shared_contact"]
    assert phone, "两家共用同一手机号应产出证据"
    assert phone[0].severity == "high"
    assert phone[0].evidence_data["value"] == "13800138000"


def test_different_phone_numbers_are_not_evidence():
    facts_a = extract_facts([_block("授权代表：王强 手机：13800138000")])
    facts_b = extract_facts([_block("联系人：李强 手机：13900139000", "c2", "乙")])
    drafts = build_fact_conflict_evidences(facts_a, facts_b, ("c1", "甲"), ("c2", "乙"))
    assert not [d for d in drafts if d.subtype == "shared_contact"]


def test_unusual_duration_match_is_flagged_but_round_numbers_are_not():
    unusual_a = extract_facts([_block("工期：137 日历天")])
    unusual_b = extract_facts([_block("工期：137 日历天", "c2", "乙")])
    drafts = build_fact_conflict_evidences(
        unusual_a, unusual_b, ("c1", "甲"), ("c2", "乙")
    )
    assert [d for d in drafts if d.subtype == "unusual_duration_match"]

    # 整月/整年工期是常见规定口径，不报
    common_a = extract_facts([_block("工期：12 个月")])
    common_b = extract_facts([_block("工期：12 个月", "c2", "乙")])
    drafts = build_fact_conflict_evidences(
        common_a, common_b, ("c1", "甲"), ("c2", "乙")
    )
    assert not [d for d in drafts if d.subtype == "unusual_duration_match"]


def test_role_roster_match_requires_three_roles():
    text = "项目负责人：张伟 技术负责人：李娜 授权代表：王强 造价人员：赵敏"
    facts_a = extract_facts([_block(text, "c1", "甲")])
    facts_b = extract_facts([_block(text, "c2", "乙")])
    drafts = build_fact_conflict_evidences(facts_a, facts_b, ("c1", "甲"), ("c2", "乙"))
    roster = [d for d in drafts if d.subtype == "role_roster_match"]
    assert roster, "整组岗位人员一致应产出证据"
    assert roster[0].severity == "high"
    assert len(roster[0].evidence_data["matched_roles"]) >= 3


def test_two_matching_roles_are_not_a_roster_match():
    text_a = "项目负责人：张伟 技术负责人：李娜 授权代表：王强"
    text_b = "项目负责人：张伟 技术负责人：李娜 授权代表：陈军"
    drafts = build_fact_conflict_evidences(
        extract_facts([_block(text_a)]),
        extract_facts([_block(text_b, "c2", "乙")]),
        ("c1", "甲"),
        ("c2", "乙"),
    )
    assert not [d for d in drafts if d.subtype == "role_roster_match"]


# --------------------------------------------- 批次共性 rsid 排除（防误报）


def test_batch_common_rsid_is_subtracted():
    """程序化生成的 docx 共享固定 rsid，必须扣除，否则每对企业都误报。"""
    from app.services.analysis_service import _batch_common_rsid

    snapshot = {
        "c1": {"files": [{"doc_metadata": {"rsid_ids": ["A", "B", "C"]}}]},
        "c2": {"files": [{"doc_metadata": {"rsid_ids": ["A", "B", "C"]}}]},
        "c3": {"files": [{"doc_metadata": {"rsid_ids": ["A", "B", "C"]}}]},
    }
    assert _batch_common_rsid(snapshot) == {"A", "B", "C"}


def test_batch_common_rsid_keeps_distinguishing_ids():
    from app.services.analysis_service import _batch_common_rsid

    snapshot = {
        "c1": {"files": [{"doc_metadata": {"rsid_ids": ["A", "B", "C", "X"]}}]},
        "c2": {"files": [{"doc_metadata": {"rsid_ids": ["A", "B", "C", "X"]}}]},
        "c3": {"files": [{"doc_metadata": {"rsid_ids": ["A", "B", "C"]}}]},
    }
    # 只有三家都有的 A/B/C 算共性；甲乙额外共享的 X 保留为线索
    assert _batch_common_rsid(snapshot) == {"A", "B", "C"}


def test_batch_common_rsid_skipped_for_two_companies():
    """只有两家时无多数派可比，不做扣除，避免把真实共享也扣掉。"""
    from app.services.analysis_service import _batch_common_rsid

    snapshot = {
        "c1": {"files": [{"doc_metadata": {"rsid_ids": ["A", "B"]}}]},
        "c2": {"files": [{"doc_metadata": {"rsid_ids": ["A", "B"]}}]},
    }
    assert _batch_common_rsid(snapshot) == set()


def test_unrelated_programmatic_docs_share_all_rsids(tmp_path):
    """记录已知行为：无关的 python-docx 文档 rsid 完全相同，靠批次扣除兜底。"""
    from docx import Document

    ids = []
    for name in ("a", "b", "c"):
        document = Document()
        document.add_paragraph(f"完全不同的内容 {name}")
        path = tmp_path / f"{name}.docx"
        document.save(str(path))
        ids.append(rsid.extract_rsid(path).all_ids)
    # 三份文档的 rsid 集合相同——这正是必须先扣除批次共性的原因
    assert ids[0] == ids[1] == ids[2]


def test_pair_price_count_includes_line_item_evidence(db):
    """逐项单价证据挂在组合上，必须计入价格列，不能凭空消失。"""
    from app.models import (
        AnalysisTask,
        ComparisonEvidence,
        ComparisonPair,
        Project,
    )
    from app.seed import seed_demo
    from app.services import results_service

    seed_demo(db)
    project = db.scalars(select(Project)).first()
    task = db.scalars(select(AnalysisTask)).first()
    pair = db.scalars(
        select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id)
    ).first()

    before = results_service.overview(db, project)
    before_count = next(
        row["price_count"] for row in before["pairs"] if row["pair_id"] == pair.id
    )

    db.add(
        ComparisonEvidence(
            project_id=project.id,
            analysis_task_id=task.id,
            comparison_pair_id=pair.id,
            evidence_type="price",
            evidence_subtype="line_item_duplicate",
            severity="high",
            summary="逐项单价相同",
            evidence_data={"identical_count": 6},
            algorithm_version="test",
        )
    )
    db.commit()

    after = results_service.overview(db, project)
    after_count = next(
        row["price_count"] for row in after["pairs"] if row["pair_id"] == pair.id
    )
    assert after_count == before_count + 1
