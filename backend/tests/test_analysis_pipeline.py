from sqlalchemy import select

from app.models import (
    AnalysisTask,
    ComparisonEvidence,
    ComparisonPair,
    Project,
    TextSimilarityResult,
)
from app.models.enums import EvidenceType, TaskStatus
from app.seed import seed_demo
from app.services import analysis_service, results_service


def test_seed_pipeline_produces_traceable_results(db):
    seed_demo(db)

    project = db.scalars(select(Project)).first()
    task = db.scalars(select(AnalysisTask)).first()
    assert project is not None
    assert task is not None
    assert task.status in {TaskStatus.COMPLETED.value, TaskStatus.PARTIAL.value}

    pairs = list(db.scalars(select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id)))
    assert len(pairs) == 3  # 3 家企业 -> C(3,2) = 3 组

    evidences = list(
        db.scalars(
            select(ComparisonEvidence).where(ComparisonEvidence.analysis_task_id == task.id)
        )
    )
    assert any(e.evidence_type == EvidenceType.TEXT.value for e in evidences)
    assert any(e.evidence_type == EvidenceType.PERSONNEL.value for e in evidences)
    assert any(e.evidence_type == EvidenceType.PRICE.value for e in evidences)

    text_evidence = next(e for e in evidences if e.evidence_type == EvidenceType.TEXT.value)
    assert text_evidence.source_a["file_id"]
    assert text_evidence.source_a["page_index"] >= 1
    assert text_evidence.source_a["start_offset"] >= 0
    assert text_evidence.evidence_data["matched_chars"] > 0

    assert any(p.text_risk in {"low", "medium", "high"} for p in pairs)
    assert any(p.price_risk in {"low", "medium", "high"} for p in pairs)

    text_results = list(
        db.scalars(
            select(TextSimilarityResult).where(TextSimilarityResult.analysis_task_id == task.id)
        )
    )
    assert text_results
    assert all(result.union_size >= result.intersection_size for result in text_results)
    assert all(0 <= result.jaccard_similarity <= 1 for result in text_results)
    assert all(result.chunk_a_id and result.chunk_b_id for result in text_results)

    content, filename = results_service.export_excel(db, project)
    assert len(content) > 5000
    assert filename.endswith(".xlsx")


def test_retry_pair_regenerates_text_results(db):
    seed_demo(db)
    task = db.scalars(select(AnalysisTask)).first()
    pair = db.scalars(
        select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id)
    ).first()

    analysis_service.retry_pair(db, task, pair)
    db.refresh(pair)

    assert pair.status == "completed"
    assert pair.error_message is None
    current_text = [
        evidence
        for evidence in pair.evidences
        if evidence.is_current and evidence.evidence_type == "text"
    ]
    assert current_text
    assert all(evidence.source_a.get("ranges") for evidence in current_text)
    results = list(
        db.scalars(
            select(TextSimilarityResult).where(TextSimilarityResult.pair_id == pair.id)
        )
    )
    assert results


def test_overview_and_review_flow(db):
    from app.core.security import hash_password
    from app.models import User
    from app.services import results_service

    seed_demo(db)
    project = db.scalars(select(Project)).first()
    overview = results_service.overview(db, project)
    assert overview["stats"]["pair_count"] == 3
    assert overview["stats"]["text_evidence_count"] >= 1
    assert len(overview["matrix"]) == 3

    evidence = db.scalars(select(ComparisonEvidence)).first()
    reviewer = User(
        username="reviewer",
        password_hash=hash_password("secret"),
        display_name="审核员",
        role="reviewer",
    )
    db.add(reviewer)
    db.commit()

    results_service.apply_review(db, evidence, "confirmed", "复核确认", reviewer)
    db.refresh(evidence)
    assert evidence.review_status == "confirmed"
    assert len(evidence.review_records) == 1

    results_service.apply_review(db, evidence, "excluded", "二次复核排除", reviewer)
    db.refresh(evidence)
    assert evidence.review_status == "excluded"
    assert len(evidence.review_records) == 2
    assert evidence.review_records[0].old_status == "pending"
