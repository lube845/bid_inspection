"""审核流程：证据级分派、单级审核、按线索审核。"""

from __future__ import annotations

import pytest

from app.models import Company, ComparisonEvidence, Project, ReviewRecord, User
from app.models.enums import UserRole
from app.services import results_service, risk_service


@pytest.fixture()
def world(db):
    project = Project(project_name="审核项目")
    db.add(project)
    db.flush()
    company = Company(project_id=project.id, company_name="甲公司")
    db.add(company)
    reviewer_a = User(
        username="reviewer_a",
        employee_no="U001",
        password_hash="x",
        display_name="审核甲",
        role=UserRole.USER.value,
        is_active=True,
    )
    reviewer_b = User(
        username="reviewer_b",
        employee_no="U002",
        password_hash="x",
        display_name="审核乙",
        role=UserRole.USER.value,
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


def test_single_review_is_final(db, world):
    """审核是单级的：一次审核即结论，不需要第二个人复核。"""
    evidence = world["evidence"]()
    results_service.apply_review(db, evidence, "confirmed", "确认", world["a"])

    assert evidence.review_status == "confirmed"
    detail = results_service.evidence_detail(db, evidence)
    assert detail["review_status"] == "confirmed"
    assert "review_stage" not in detail
    assert "first_review" not in detail
    assert detail["review_records"][0]["new_status"] == "confirmed"


def test_anyone_can_change_a_previous_conclusion(db, world):
    """结论可以被任何人改判，包括提出结论的人自己。"""
    evidence = world["evidence"]()
    results_service.apply_review(db, evidence, "confirmed", "先确认", world["a"])
    results_service.apply_review(db, evidence, "excluded", "后发现是正常", world["a"])

    assert evidence.review_status == "excluded"
    assert len(evidence.review_records) == 2


def test_pending_reverts_the_decision(db, world):
    evidence = world["evidence"]()
    results_service.apply_review(db, evidence, "confirmed", "确认", world["a"])
    results_service.apply_review(db, evidence, "pending", "撤回", world["a"])

    assert evidence.review_status == "pending"


def test_batch_review_is_final(db, world):
    evidence = world["evidence"]()
    result = risk_service.batch_review(db, [evidence.id], "undecided", "批量", world["a"])

    assert result["updated"] == 1
    assert evidence.review_status == "undecided"


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
        records = db.query(ReviewRecord).filter(ReviewRecord.evidence_id == item.id).all()
        assert len(records) == 1


def test_cluster_review_can_change_conclusion(db, world):
    """同一个人可以再按线索改判，不需要换人复核。"""
    lead = world["evidence"](cluster_id="c2", cluster_size=2, cluster_lead_id=None)
    member = world["evidence"](cluster_id="c2", cluster_size=2, cluster_lead_id=lead.id)
    db.commit()
    risk_service.cluster_review(db, [lead.id, member.id], "confirmed", "确认", world["a"])

    result = risk_service.cluster_review(db, [lead.id, member.id], "excluded", "改判", world["a"])
    assert result["updated"] == 2
    # 服务层不提交事务：这里显式 commit 后再读，确保拿到落库的最终状态
    db.commit()
    for item in (lead, member):
        db.refresh(item)
        assert item.review_status == "excluded"


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
    reviewed = world["evidence"]()
    results_service.apply_review(db, reviewed, "confirmed", "确认", world["a"])
    db.commit()

    project_id = world["project"].id
    pending_rows = risk_service.list_risks(db, project_id=project_id, review_queue="pending")
    reviewed_rows = risk_service.list_risks(db, project_id=project_id, review_queue="reviewed")
    assert [row["id"] for row in pending_rows["items"]] == [pending.id]
    assert [row["id"] for row in reviewed_rows["items"]] == [reviewed.id]
