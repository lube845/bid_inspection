"""风险管理（PRD 4.11）测试。

重点覆盖：
- 证据编号格式（PRD 5.7）；
- 企业过滤用 IN 子查询而非 JOIN，total 不被参与人数放大；
- 审核人取最新一条审核记录；
- 批量审核只允许「待进一步核查」。
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models import (
    AnalysisTask,
    Company,
    ComparisonEvidence,
    EvidenceParticipant,
    Project,
    ReviewRecord,
    User,
)
from app.models.enums import EvidenceType, ReviewStatus, RiskLevel
from app.models.base import utcnow
from app.services import risk_service
from app.core.security import hash_password


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
    companies = []
    for name in ("甲公司", "乙公司", "丙公司"):
        company = Company(project_id=project.id, company_name=name, participate_analysis=True)
        db.add(company)
        companies.append(company)
    db.commit()
    return project, task, companies


def _evidence(db, project, task, evidence_type, participants=(), severity="medium", created_at=None):
    record = ComparisonEvidence(
        project_id=project.id,
        analysis_task_id=task.id,
        comparison_pair_id=None,
        evidence_type=evidence_type,
        evidence_subtype="sub",
        severity=severity,
        summary="摘要",
        evidence_data={},
        source_a=None,
        source_b=None,
        algorithm_version="test",
        review_status=ReviewStatus.PENDING.value,
        is_current=True,
        created_at=created_at or utcnow(),
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
    project, task, companies = _world(db)
    _evidence(db, project, task, EvidenceType.TEXT.value)
    _evidence(db, project, task, EvidenceType.TEXT.value)

    result = risk_service.list_risks(db)
    codes = sorted(item["evidence_code"] for item in result["items"])
    assert codes == ["DEMO-2026-001-T03-TEXT-0001", "DEMO-2026-001-T03-TEXT-0002"]


def test_evidence_code_restarts_per_type(db):
    project, task, companies = _world(db)
    _evidence(db, project, task, EvidenceType.TEXT.value)
    _evidence(db, project, task, EvidenceType.PRICE.value)

    result = risk_service.list_risks(db)
    codes = sorted(item["evidence_code"] for item in result["items"])
    assert codes == ["DEMO-2026-001-T03-PRICE-0001", "DEMO-2026-001-T03-TEXT-0001"]


def test_company_filter_does_not_inflate_total(db):
    """涉及 3 家企业的价格证据，按其中任意一家过滤都要命中，且 total 只算 1 条。"""
    project, task, companies = _world(db)
    _evidence(
        db,
        project,
        task,
        EvidenceType.PRICE.value,
        participants=companies,
    )

    for company in companies:
        result = risk_service.list_risks(db, company_id=company.id)
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["companies"] == ["甲公司", "乙公司", "丙公司"]


def test_pagination_total_is_filtered_count(db):
    project, task, companies = _world(db)
    for _ in range(5):
        _evidence(db, project, task, EvidenceType.TEXT.value)
    _evidence(db, project, task, EvidenceType.PRICE.value, participants=companies)

    page = risk_service.list_risks(db, evidence_type=EvidenceType.TEXT.value, page=1, page_size=2)
    assert page["total"] == 5
    assert len(page["items"]) == 2
    assert page["page"] == 1
    assert page["page_size"] == 2

    page2 = risk_service.list_risks(db, evidence_type=EvidenceType.TEXT.value, page=3, page_size=2)
    assert len(page2["items"]) == 1


def test_stale_evidence_is_excluded(db):
    project, task, companies = _world(db)
    stale = _evidence(db, project, task, EvidenceType.TEXT.value)
    stale.is_current = False
    db.commit()

    result = risk_service.list_risks(db)
    assert result["total"] == 0
    assert result["items"] == []


def test_reviewer_is_latest_review_record(db):
    project, task, companies = _world(db)
    evidence = _evidence(db, project, task, EvidenceType.TEXT.value)
    first = User(username="first", password_hash=hash_password("x"), display_name="第一审核人", role="reviewer")
    second = User(username="second", password_hash=hash_password("x"), display_name="第二审核人", role="reviewer")
    db.add_all([first, second])
    db.commit()
    db.add(ReviewRecord(evidence_id=evidence.id, reviewer_id=first.id, reviewer_name="第一审核人",
                        old_status="pending", new_status="confirmed"))
    db.commit()
    db.add(ReviewRecord(evidence_id=evidence.id, reviewer_id=second.id, reviewer_name="第二审核人",
                        old_status="confirmed", new_status="excluded"))
    db.commit()

    result = risk_service.list_risks(db)
    assert result["items"][0]["reviewer_name"] == "第二审核人"

    filtered = risk_service.list_risks(db, reviewer_id=second.id)
    assert filtered["total"] == 1
    filtered = risk_service.list_risks(db, reviewer_id=first.id)
    assert filtered["total"] == 1


def test_batch_review_rejects_confirm_and_exclude(db):
    project, task, companies = _world(db)
    evidence = _evidence(db, project, task, EvidenceType.TEXT.value)
    reviewer = User(username="reviewer", password_hash=hash_password("x"), display_name="审核人", role="reviewer")
    db.add(reviewer)
    db.commit()

    for status in ("confirmed", "excluded"):
        try:
            risk_service.batch_review(db, [evidence.id], status, None, reviewer)
        except risk_service.RiskValidationError as exc:
            assert "待进一步核查" in str(exc)
        else:
            raise AssertionError(f"批量 {status} 应当被拒绝")

    db.rollback()
    db.refresh(evidence)
    assert evidence.review_status == ReviewStatus.PENDING.value


def test_batch_review_marks_undecided(db):
    project, task, companies = _world(db)
    evidence = _evidence(db, project, task, EvidenceType.TEXT.value)
    reviewer = User(username="reviewer", password_hash=hash_password("x"), display_name="审核人", role="reviewer")
    db.add(reviewer)
    db.commit()

    result = risk_service.batch_review(db, [evidence.id], "undecided", "需要进一步核查", reviewer)
    db.commit()
    assert result["updated"] == 1
    db.refresh(evidence)
    assert evidence.review_status == ReviewStatus.UNDECIDED.value
    records = list(db.scalars(select(ReviewRecord).where(ReviewRecord.evidence_id == evidence.id)))
    assert len(records) == 1
    assert records[0].comment == "需要进一步核查"


def test_risk_detail_includes_code_and_files(db):
    project, task, companies = _world(db)
    evidence = _evidence(db, project, task, EvidenceType.TEXT.value, participants=companies[:2])
    detail = risk_service.risk_detail(db, evidence)
    assert detail["evidence_code"] == "DEMO-2026-001-T03-TEXT-0001"
    assert detail["project_name"] == "风险项目"
    assert detail["task_no"] == 3
    assert detail["companies"] == ["甲公司", "乙公司"]


def test_risks_endpoint_requires_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/v1/risks").status_code == 401
        assert client.get("/api/v1/risks/filters").status_code == 401
