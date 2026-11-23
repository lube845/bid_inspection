"""证据编号（PRD 5.7）与详情富信息测试。

编号原先由风险模块提供，审核页删除后迁入 ``results_service``：Excel 报告、
证据详情弹窗都读同一个 ``evidence_codes``，两处编号必须完全一致。
"""

from __future__ import annotations

from app.models import (
    AnalysisTask,
    Company,
    ComparisonEvidence,
    EvidenceParticipant,
    Project,
    User,
)
from app.models.enums import EvidenceType, ReviewStatus
from app.services import results_service


def _world(db):
    project = Project(project_name="风险项目", project_code="DEMO-2026-001", status="analyzed")
    db.add(project)
    db.commit()
    task = AnalysisTask(
        project_id=project.id,
        task_no=3,
        status="completed",
        analysis_config={"dimensions": ["text"], "thresholds": {}},
        company_snapshot={"companies": []},
        algorithm_version="test",
        progress={},
    )
    db.add(task)
    db.commit()
    return project, task


def _evidence(db, project, task, evidence_type, participants=(), severity="medium"):
    record = ComparisonEvidence(
        project_id=project.id,
        analysis_task_id=task.id,
        comparison_pair_id=None,
        evidence_type=evidence_type,
        evidence_subtype="sub",
        severity=severity,
        summary="摘要",
        evidence_data={},
        source_a={"file_id": "f1", "file_name": "标书.pdf", "page_index": 2},
        source_b=None,
        algorithm_version="test",
        review_status=ReviewStatus.PENDING.value,
        is_current=True,
    )
    db.add(record)
    db.flush()
    for company in participants:
        db.add(
            EvidenceParticipant(
                evidence_id=record.id,
                company_id=company.id,
                company_name=company.company_name,
                role="participant",
                source=None,
            )
        )
    db.commit()
    return record


def test_evidence_code_follows_prd_format(db):
    project, task = _world(db)
    first = _evidence(db, project, task, EvidenceType.TEXT.value)
    second = _evidence(db, project, task, EvidenceType.TEXT.value)

    codes = results_service.evidence_codes(db, [first, second])
    assert sorted(codes.values()) == [
        "DEMO-2026-001-T03-TEXT-0001",
        "DEMO-2026-001-T03-TEXT-0002",
    ]


def test_evidence_code_restarts_per_type(db):
    project, task = _world(db)
    text = _evidence(db, project, task, EvidenceType.TEXT.value)
    price = _evidence(db, project, task, EvidenceType.PRICE.value)

    codes = results_service.evidence_codes(db, [text, price])
    assert sorted(codes.values()) == [
        "DEMO-2026-001-T03-PRICE-0001",
        "DEMO-2026-001-T03-TEXT-0001",
    ]


def test_detail_code_matches_export_code(db):
    """详情弹窗（include_code）与批量编号必须给出同一个编号。"""
    project, task = _world(db)
    evidence = _evidence(db, project, task, EvidenceType.TEXT.value)
    _evidence(db, project, task, EvidenceType.TEXT.value)

    batch_code = results_service.evidence_codes(db, [evidence])[evidence.id]
    detail = results_service.evidence_detail(db, evidence, include_code=True)
    assert detail["evidence_code"] == batch_code

    # 默认不计算编号：三栏每次点选证据都调这个接口，避免整分区窗口函数开销
    plain = results_service.evidence_detail(db, evidence)
    assert "evidence_code" not in plain


def test_detail_includes_files_and_companies(db):
    project, task = _world(db)
    company = Company(project_id=project.id, company_name="甲公司")
    db.add(company)
    db.flush()
    evidence = _evidence(db, project, task, EvidenceType.PRICE.value, participants=[company])
    reviewer = User(
        username="rev", employee_no="R001", password_hash="x", display_name="审核甲"
    )
    db.add(reviewer)
    db.commit()
    results_service.apply_review(db, evidence, "confirmed", "确认", reviewer)

    detail = results_service.evidence_detail(db, evidence, include_code=True)
    assert detail["project_name"] == "风险项目"
    assert detail["task_no"] == 3
    assert "甲公司" in detail["companies"]
    assert detail["files"], "详情应带上相关原始文件与页码"
    assert detail["reviewer_name"] == "审核甲"
    assert detail["viewer_link"].endswith("?tab=results")
