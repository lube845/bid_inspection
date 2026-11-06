from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import AnalysisTask, ComparisonEvidence, ComparisonPair, Project, User
from app.schemas.analysis import ReviewRequest
from app.services import audit, results_service

router = APIRouter(tags=["results"])


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    return project


@router.get("/projects/{project_id}/results/overview")
def results_overview(
    project_id: str,
    task_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    return results_service.overview(db, project, task_id)


@router.get("/pairs/{pair_id}")
def pair_detail(
    pair_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    pair = db.get(ComparisonPair, pair_id)
    if not pair:
        raise HTTPException(404, "企业组合不存在")
    return results_service.pair_detail(db, pair)


@router.get("/evidences/{evidence_id}")
def evidence_detail(
    evidence_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    evidence = db.get(ComparisonEvidence, evidence_id)
    if not evidence:
        raise HTTPException(404, "证据不存在")
    return results_service.evidence_detail(db, evidence)


@router.post("/evidences/{evidence_id}/review")
def review_evidence(
    evidence_id: str,
    payload: ReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    evidence = db.get(ComparisonEvidence, evidence_id)
    if not evidence:
        raise HTTPException(404, "证据不存在")
    results_service.apply_review(db, evidence, payload.status, payload.comment, user)
    return results_service.evidence_detail(db, evidence)


@router.get("/projects/{project_id}/reviews")
def review_center(
    project_id: str,
    evidence_type: str | None = None,
    severity: str | None = None,
    review_status: str | None = None,
    company_id: str | None = None,
    task_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    return results_service.list_reviews(
        db,
        project.id,
        evidence_type=evidence_type,
        severity=severity,
        review_status=review_status,
        company_id=company_id,
        task_id=task_id,
        page=page,
        page_size=page_size,
    )


@router.get("/pairs/{pair_id}/reports/excel")
def export_pair_excel(
    pair_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """导出单个企业组合的证据 Excel（比对分析页使用）。"""
    pair = db.get(ComparisonPair, pair_id)
    if not pair:
        raise HTTPException(404, "企业组合不存在")
    task = db.get(AnalysisTask, pair.analysis_task_id)
    if not task:
        raise HTTPException(404, "分析任务不存在")
    project = db.get(Project, task.project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    try:
        content, filename = results_service.export_excel(
            db, project, task_id=task.id, pair_id=pair_id
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    headers = {
        "Content-Disposition": (
            f"attachment; filename=pair-evidence.xlsx; filename*=UTF-8''{quote(filename)}"
        )
    }
    audit.log_audit(
        db,
        "report.export",
        "project",
        project.id,
        user,
        {"task_id": task.id, "scope": "pair", "pair_id": pair_id},
        entity_name=project.project_name,
    )
    db.commit()
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.get("/projects/{project_id}/reports/excel")
def export_excel(
    project_id: str,
    task_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    project = _get_project(db, project_id)
    try:
        content, filename = results_service.export_excel(db, project, task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    headers = {
        "Content-Disposition": (
            f"attachment; filename=report.xlsx; filename*=UTF-8''{quote(filename)}"
        )
    }
    audit.log_audit(
        db,
        "report.export",
        "project",
        project.id,
        user,
        {"task_id": task_id, "scope": "project"},
        entity_name=project.project_name,
    )
    db.commit()
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
