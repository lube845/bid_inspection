"""审核流程：证据级分派、二审、按线索审核与批量不绕过二审。"""

from __future__ import annotations

import pytest

from app.models import Company, ComparisonEvidence, Project, ReviewRecord, User
from app.models.enums import UserRole
from app.services import results_service, risk_service
from app.services.results_service import ReviewConflict


@pytest.fixture()
def world(db):
    project = Project(project_name="审核项目", requires_second_review=True)
    db.add(project)
    db.flush()
    company = Company(project_id=project.id, company_name="甲公司")
    db.add(company)
    reviewer_a = User(
        username="reviewer_a",
        password_hash="x",
        display_name="审核甲",
        role=UserRole.REVIEWER.value,
        is_active=True,
    )
    reviewer_b = User(
        username="reviewer_b",
        password_hash="x",
        display_name="审核乙",
        role=UserRole.REVIEWER.value,
        is_active=True,
    )
    db.add_all([reviewer_a, reviewer_b])
    db.flush()

    def evidence(**kwargs) -> ComparisonEvidence:
        item = ComparisonEvidence(
            project_id=project.id,
            analysis_task_id="task-1",
            evidence_type="text",
            evidence_subtype="text_duplicate",
            severity="high",
            summary="相似段落",
            evidence_data=kwargs.get("data") or {},
            cluster_id=kwargs.get("cluster_id"),
            cluster_size=kwargs.get("cluster_size", 1),
            cluster_lead_id=kwargs.get("cluster_lead_id"),
            review_status="pending",
        )
        db.add(item)
        db.flush()
        return item

    db.commit()
    return {
        "project": project,
        "company": company,
        "a": reviewer_a,
        "b": reviewer_b,
        "evidence": evidence,
    }


def test_first_review_awaits_second(db, world):
    evidence = world["evidence"]()
    results_service.apply_review(db, evidence, "confirmed", "一审确认", world["a"])

    assert evidence.review_status == "confirmed"
    assert evidence.review_stage == 1
    assert evidence.first_reviewer_id == world["a"].id
    assert evidence.first_review_status == "confirmed"


def test_same_reviewer_cannot_complete_second_review(db, world):
    evidence = world["evidence"]()
    results_service.apply_review(db, evidence, "confirmed", "一审", world["a"])

    with pytest.raises(ReviewConflict):
        results_service.apply_review(db, evidence, "excluded", "二审", world["a"])


def test_second_reviewer_finalizes_and_conflict_is_visible(db, world):
    evidence = world["evidence"]()
    results_service.apply_review(db, evidence, "confirmed", "一审确认", world["a"])
    results_service.apply_review(db, evidence, "excluded", "二审排除", world["b"])

    assert evidence.review_stage == 2
    detail = results_service.evidence_detail(db, evidence)
    assert detail["review_conflict"] is True
    stages = [record["stage"] for record in detail["review_records"]]
    assert stages == [1, 2]


def test_project_without_second_review_is_single_stage(db, world):
    world["project"].requires_second_review = False
    db.commit()
    evidence = world["evidence"]()
    results_service.apply_review(db, evidence, "confirmed", "确认", world["a"])

    assert evidence.review_stage == 2


def test_batch_review_does_not_require_second_review(db, world):
    evidence = world["evidence"]()
    result = risk_service.batch_review(db, [evidence.id], "undecided", "批量", world["a"])

    assert result["updated"] == 1
    assert evidence.review_stage == 2


def test_cluster_review_covers_all_members(db, world):
    lead = world["evidence"](cluster_id="c1", cluster_size=2, cluster_lead_id=None)
    member = world["evidence"](cluster_id="c1", cluster_size=2, cluster_lead_id=lead.id)
    lead.cluster_lead_id = lead.id
    db.commit()

    result = risk_service.cluster_review(
        db, [lead.id, member.id], "confirmed", "整条线索确认", world["a"]
    )

    assert result["updated"] == 2
    db.flush()
    for item in (lead, member):
        assert item.review_status == "confirmed"
        assert item.review_stage == 1
        records = db.query(ReviewRecord).filter(ReviewRecord.evidence_id == item.id).all()
        assert len(records) == 1


def test_cluster_review_rejects_self_second_review(db, world):
    lead = world["evidence"](cluster_id="c2", cluster_size=2, cluster_lead_id=None)
    member = world["evidence"](cluster_id="c2", cluster_size=2, cluster_lead_id=lead.id)
    db.commit()
    risk_service.cluster_review(db, [lead.id, member.id], "confirmed", "一审", world["a"])

    with pytest.raises(risk_service.RiskValidationError):
        risk_service.cluster_review(db, [lead.id, member.id], "excluded", "二审", world["a"])


def test_assignment_and_filters(db, world):
    evidence = world["evidence"]()
    other = world["evidence"]()

    risk_service.batch_assign(db, [evidence.id], world["a"].id, world["a"])
    db.commit()

    assert evidence.assignee_id == world["a"].id
    assert evidence.assigned_at is not None

    listing = risk_service.list_risks(db, project_id=world["project"].id, assignee_id=world["a"].id)
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == evidence.id

    risk_service.batch_assign(db, [evidence.id], None, world["a"])
    db.commit()
    assert evidence.assignee_id is None
    assert other.assignee_id is None


def test_review_queue_filter(db, world):
    pending = world["evidence"]()
    awaiting = world["evidence"]()
    finalized = world["evidence"]()
    results_service.apply_review(db, awaiting, "confirmed", "一审", world["a"])
    results_service.apply_review(db, finalized, "confirmed", "确认", world["a"])
    db.commit()
    # 模拟已终审：绕过二审的项目设置
    finalized.review_stage = 2
    db.commit()

    project_id = world["project"].id
    assert risk_service.list_risks(db, project_id=project_id, review_queue="pending_first")["total"] == 1
    assert risk_service.list_risks(db, project_id=project_id, review_queue="awaiting_second")["total"] == 1
    assert risk_service.list_risks(db, project_id=project_id, review_queue="final")["total"] == 1
    assert pending.id  # 仅用于说明三个队列各一条
