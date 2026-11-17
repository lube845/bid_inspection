"""A 组改进的回归测试：入口归一、共同模板、跨章节类型、文件取证。"""

import hashlib

from sqlalchemy import select

from app.analysis.forensics import (
    build_file_fingerprint_evidence,
    build_metadata_evidence,
)
from app.analysis.template import (
    TemplateProfile,
    build_template_profile,
    informativeness,
)
from app.models import AnalysisTask, ComparisonEvidence, Project
from app.models.enums import EvidenceType
from app.seed import seed_demo
from app.services import analysis_service, results_service
from app.utils.text import normalize

# ---------------------------------------------------------------- A1 入口归一


def test_zero_width_characters_do_not_break_similarity():
    """往词里插零宽字符是绕过查重最省力的手法，规范化必须把它们去掉。"""
    for invisible in ["​", "‌", "‍", "⁠", "﻿", "­"]:
        assert normalize(f"投{invisible}标{invisible}总报价") == "投标总报价"


def test_variation_selector_and_bidi_marks_are_dropped():
    assert normalize("投标️") == "投标"
    assert normalize("‪投标‬") == "投标"


def test_homoglyphs_fold_to_latin():
    """西里尔/希腊同形字母肉眼与打印都无法区分，必须映射回拉丁字母。"""
    assert normalize("аbc") == "abc"  # 西里尔 а
    assert normalize("Οffice") == "office"  # 希腊 Ο
    assert normalize("КV-12") == "kv12"  # 西里尔 К
    # 真正的中文不应被改写
    assert normalize("投标报价") == "投标报价"


def test_normalization_still_keeps_numbers():
    """入口归一不能顺手改掉数字语义（数字感知规则已被否决）。"""
    assert normalize("3.15") == "315"
    assert normalize("１，２３４") == "1234"


# ------------------------------------------------------------ A4 共同模板


def test_informativeness_separates_boilerplate_from_specs():
    boilerplate = (
        "我方将严格按照招标文件要求，科学组织施工，合理配置人员设备，"
        "确保工程按期保质完成，建立健全质量管理体系。"
    )
    spec = (
        "采用 DN300 球墨铸铁管，公称压力 1.6MPa，壁厚 ≥8.0mm，"
        "接口形式 K 型柔性接口，抗压强度 ≥420MPa，执行 GB/T 13295-2019。"
    )
    assert informativeness(boilerplate) < 0.12
    assert informativeness(spec) > 0.12


def test_template_profile_counts_cross_company_presence():
    """同一段套话出现在 3 家企业时，共现数应为 3；企业内重复不计。"""
    shared = "我方将严格按照招标文件要求，科学组织施工，合理配置人员设备，确保工程按期保质完成。"

    class FakeChunk:
        def __init__(self, company_id, text, index):
            self.company_id = company_id
            self.chunk_id = f"{company_id}:{index}"
            self.normalized = text

    chunks = {
        "a": [FakeChunk("a", shared, 0), FakeChunk("a", shared, 1)],
        "b": [FakeChunk("b", shared, 0)],
        "c": [FakeChunk("c", shared, 0)],
        "d": [FakeChunk("d", "完全不同的一段内容" * 20, 0)],
    }
    profile = build_template_profile(chunks, similarity=0.6, shingle_size=3)
    assert profile.presence["a:0"] == 3
    assert profile.presence["b:0"] == 3
    assert profile.presence["d:0"] == 1


def test_template_requires_min_companies_and_low_informativeness():
    profile = TemplateProfile(
        presence={"a": 3, "b": 3, "x": 2, "y": 3},
        informativeness={"a": 0.01, "b": 0.02, "x": 0.01, "y": 0.5},
    )
    kwargs = dict(min_companies=3, max_informativeness=0.12, max_chars_guard=200)
    # 两家企业共现：不够
    assert not profile.is_template("a", "x", 100, **kwargs)
    # 三条门槛都满足
    assert profile.is_template("a", "b", 100, **kwargs)
    # 信息量高：是实质内容，不当模板
    assert not profile.is_template("a", "y", 100, **kwargs)
    # 长段逐字雷同：保留为证据
    assert not profile.is_template("a", "b", 400, **kwargs)


def test_long_verbatim_copies_are_never_downgraded_to_template():
    """几百字逐字相同本身就是强证据，不能因为「多家都这么写」被抹掉。"""
    profile = TemplateProfile(presence={"a": 5, "b": 5}, informativeness={"a": 0.0, "b": 0.0})
    assert not profile.is_template(
        "a", "b", 500, min_companies=3, max_informativeness=0.2, max_chars_guard=200
    )


# ------------------------------------------------------- A5 跨章节类型


def _chunk(content_type: str, text: str):
    class FakeChunk:
        def __init__(self):
            self.content_type = content_type
            self.normalized = text
            self.company_id = content_type
            self.chunk_id = f"{content_type}:{text[:4]}"

    return FakeChunk()


def test_cross_type_short_hit_is_noise_but_long_hit_is_evidence():
    """同一段套话同时出现在技术标和商务标里，短命中多为通用表述。"""
    # 直接验证判定函数的行为，通过 match_chunks 的可观测结果覆盖
    from app.analysis.text_similarity import match_chunks

    technical = _chunk("technical", "我方将严格按照招标文件要求组织施工" * 3)
    commercial = _chunk("commercial", "我方将严格按照招标文件要求组织施工" * 3)
    matches, stats = match_chunks(
        [(technical, commercial, 3)], None, thresholds={"text_min_match_chars": 5}
    )
    # 短命中被跨类型过滤掉
    assert stats["cross_type_skipped"] >= 1
    assert matches == []

    long_text = "我方将严格按照招标文件要求组织施工，合理配置人员和设备。" * 10
    technical = _chunk("technical", long_text)
    commercial = _chunk("commercial", long_text)
    matches, stats = match_chunks(
        [(technical, commercial, 3)], None, thresholds={"text_min_match_chars": 5}
    )
    # 整段搬家反而说明连章节归属都抄错了，必须保留
    assert matches, "长段跨类型雷同应保留为证据"


# --------------------------------------------------------- A2/A3 文件取证


def test_identical_file_evidence_is_high_risk():
    file_a = {"file_id": "f1", "file_name": "甲.pdf", "file_hash": "abc123"}
    file_b = {"file_id": "f2", "file_name": "乙.pdf", "file_hash": "abc123"}
    draft = build_file_fingerprint_evidence(("c1", "甲公司"), ("c2", "乙公司"), file_a, file_b)
    assert draft.evidence_type == EvidenceType.FILE.value
    assert draft.subtype == "identical_file"
    assert draft.severity == "high"
    assert "abc123"[:12] in draft.summary


def test_metadata_evidence_requires_identifying_fields():
    """只有 Producer 相同不算证据：同一批办公软件导出是常态。"""
    file_a = {"file_id": "f1", "file_name": "甲.pdf", "metadata": {"producer": "WPS Office"}}
    file_b = {"file_id": "f2", "file_name": "乙.pdf", "metadata": {"producer": "WPS Office"}}
    assert build_metadata_evidence(("c1", "甲"), ("c2", "乙"), file_a, file_b) is None


def test_metadata_evidence_flags_shared_author():
    file_a = {
        "file_id": "f1",
        "file_name": "甲.docx",
        "metadata": {"author": "李四", "last_modified_by": "李四"},
    }
    file_b = {
        "file_id": "f2",
        "file_name": "乙.docx",
        "metadata": {"author": "李四", "last_modified_by": "李四"},
    }
    draft = build_metadata_evidence(("c1", "甲"), ("c2", "乙"), file_a, file_b)
    assert draft is not None
    assert draft.evidence_type == EvidenceType.METADATA.value
    assert draft.evidence_data["shared_fields"]["author"] == "李四"


def test_placeholder_author_is_not_evidence():
    file_a = {"file_id": "f1", "file_name": "甲.docx", "metadata": {"author": "Administrator"}}
    file_b = {"file_id": "f2", "file_name": "乙.docx", "metadata": {"author": "Administrator"}}
    assert build_metadata_evidence(("c1", "甲"), ("c2", "乙"), file_a, file_b) is None


# ------------------------------------------------------- 端到端：同一份文件


def test_identical_submitted_file_is_detected_end_to_end(db):
    """两家企业提交同一份文件时，必须产生文件指纹证据，且计入综合风险。"""
    seed_demo(db)
    task = db.scalars(select(AnalysisTask)).first()
    assert task is not None

    # 把乙公司的文件哈希改成与甲公司相同，模拟「同一份文件被两家提交」
    snapshot = dict(task.company_snapshot)
    companies = snapshot["companies"]
    snap_a = companies[0]
    snap_b = companies[1]
    shared_hash = snap_a["files"][0]["file_hash"]
    for file in snap_b["files"]:
        file["file_hash"] = shared_hash
    task.company_snapshot = {"companies": companies, **{
        key: value for key, value in snapshot.items() if key != "companies"
    }}
    db.commit()

    pair = next(p for p in task.pairs if p.status == "completed")
    analysis_service.retry_pair(db, task, pair)

    evidences = list(
        db.scalars(
            select(ComparisonEvidence).where(
                ComparisonEvidence.analysis_task_id == task.id,
                ComparisonEvidence.is_current.is_(True),
            )
        )
    )
    forensic = [e for e in evidences if e.evidence_type == EvidenceType.FILE.value]
    assert forensic, "同一份文件应产生文件指纹证据"
    assert any(e.severity == "high" for e in forensic)

    # 回归：价格阶段会重算 overall_risk，不能把取证风险的 high 覆盖掉
    db.refresh(pair)
    assert pair.forensic_risk == "high"
    assert pair.overall_risk == "high", "取证风险必须参与综合风险"


def test_forensic_risk_survives_price_recalculation(db):
    """价格阶段重算综合风险时，必须带上取证维度。"""
    from app.models import ComparisonPair
    from app.seed import seed_demo

    seed_demo(db)
    task = db.scalars(select(AnalysisTask)).first()
    pair = db.scalars(
        select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id)
    ).first()
    pair.forensic_risk = "high"
    pair.text_risk = "low"
    pair.personnel_risk = "none"
    pair.price_risk = "none"
    db.commit()

    analysis_service._apply_price_risk_to_pairs(db, task)
    db.refresh(pair)
    assert pair.overall_risk == "high"


def test_overview_reports_forensic_counts(db):
    seed_demo(db)
    project = db.scalars(select(Project)).first()
    data = results_service.overview(db, project)
    stats = data["stats"]
    assert "file_evidence_count" in stats
    assert "metadata_evidence_count" in stats
    assert "template_text_count" in stats


def test_missing_text_params_are_added_without_overwriting(db):
    """新增参数要能在规则页看到，但管理员调过的旧参数不能被覆盖。"""
    from app.services import rule_service

    rule_service.ensure_initial_version(db)
    db.commit()
    active = rule_service.active_version(db, "text")
    params = dict(active.params)
    # 模拟旧版本：删掉新参数，并把一个旧参数改成管理员自定义值
    for key in (
        "template_min_companies",
        "template_max_informativeness",
        "template_max_chars_guard",
        "text_cross_type_guard_chars",
    ):
        params.pop(key, None)
    params["text_similarity"] = 0.77
    active.params = params
    db.commit()

    patched = rule_service.ensure_missing_text_params(db)
    db.commit()
    assert patched is not None
    merged = patched.params
    assert merged["template_min_companies"] == 3
    assert merged["text_cross_type_guard_chars"] == 120
    # 自定义值保留
    assert merged["text_similarity"] == 0.77
    # 幂等：再跑一次不再新建版本
    assert rule_service.ensure_missing_text_params(db) is None
