from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_project
from app.db.session import get_db
from app.models import AnalysisTask, ComparisonEvidence, ComparisonPair, Project, User
from app.schemas.analysis import ReviewRequest
from app.services import access_service, audit, results_service, template_service

router = APIRouter(tags=["results"])


def _get_evidence(
    db: Session, evidence_id: str, user: User, role: str = access_service.VIEW
) -> ComparisonEvidence:
    """证据、企业组合都挂在项目下，权限一律以所属项目为准。"""
    evidence = db.get(ComparisonEvidence, evidence_id)
    if not evidence:
        raise HTTPException(404, "证据不存在")
    get_project(db, evidence.project_id, user, role)
    return evidence


def _get_pair(db: Session, pair_id: str, user: User, role: str = access_service.VIEW) -> ComparisonPair:
    pair = db.get(ComparisonPair, pair_id)
    if not pair:
        raise HTTPException(404, "企业组合不存在")
    _get_task_project(db, pair.analysis_task_id, user, role)
    return pair


def _get_task_project(
    db: Session, task_id: str, user: User, role: str = access_service.VIEW
) -> Project:
    task = db.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(404, "分析任务不存在")
    return get_project(db, task.project_id, user, role)


@router.get("/projects/{project_id}/results/overview")
def results_overview(
    project_id: str,
    task_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = get_project(db, project_id, user)
    return results_service.overview(db, project, task_id)


@router.get("/projects/{project_id}/tasks/{task_id}/diff")
def task_diff(
    project_id: str,
    task_id: str,
    against: str | None = Query(default=None, description="对比的上一次任务 id，缺省取上一任务"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = get_project(db, project_id, user)
    try:
        return results_service.task_diff(db, project, task_id, against)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/pairs/{pair_id}")
def pair_detail(
    pair_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    pair = _get_pair(db, pair_id, user)
    return results_service.pair_detail(db, pair)


@router.get("/pairs/{pair_id}/evidences")
def pair_evidences(
    pair_id: str,
    evidence_type: str | None = None,
    review_status: str | None = None,
    severity: str | None = None,
    tender: str | None = None,
    cluster_lead_only: bool = False,
    min_cluster_size: int | None = Query(default=None, ge=1),
    review_queue: str | None = Query(default=None, pattern="^(pending|reviewed)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    before: int = Query(default=100, ge=0, le=2000),
    after: int = Query(default=100, ge=0, le=2000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """按需返回证据及默认上下文。

    比对分析页首屏只拉取这一份数据，左右两列不再默认加载完整原文。
    """
    pair = _get_pair(db, pair_id, user)
    return results_service.pair_evidences(
        db,
        pair,
        evidence_type=evidence_type,
        review_status=review_status,
        severity=severity,
        tender=tender,
        cluster_lead_only=cluster_lead_only,
        min_cluster_size=min_cluster_size,
        review_queue=review_queue,
        page=page,
        page_size=page_size,
        before=before,
        after=after,
    )


@router.get("/evidences/{evidence_id}")
def evidence_detail(
    evidence_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    evidence = _get_evidence(db, evidence_id, user)
    return results_service.evidence_detail(db, evidence)


@router.get("/evidences/{evidence_id}/context")
def evidence_context(
    evidence_id: str,
    side: str = Query(default="a", pattern="^[ab]$"),
    before: int = Query(default=100, ge=0, le=2000),
    after: int = Query(default=100, ge=0, le=2000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """按需读取证据某一侧的扩展上下文。"""
    evidence = _get_evidence(db, evidence_id, user)
    try:
        return results_service.evidence_context_payload(db, evidence, side, before, after)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/evidences/{evidence_id}/review")
def review_evidence(
    evidence_id: str,
    payload: ReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    evidence = _get_evidence(db, evidence_id, user, access_service.REVIEW)
    template_fragment_id = None
    if payload.learn_as_template:
        if payload.status != "excluded":
            raise HTTPException(400, "只有「正常 / 排除」的结论才能同时记为模板")
        if payload.template_scope == "global" and user.role != "admin":
            raise HTTPException(403, "全局模板只能由系统管理员维护")
        try:
            fragment = template_service.learn_from_evidence(
                db,
                evidence,
                user,
                scope=payload.template_scope,
            )
        except template_service.TemplateError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        template_fragment_id = fragment.id
    results_service.apply_review(
        db,
        evidence,
        payload.status,
        payload.comment,
        user,
        template_fragment_id=template_fragment_id,
    )
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
    project = get_project(db, project_id, user)
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
    pair = _get_pair(db, pair_id, user)
    task = db.get(AnalysisTask, pair.analysis_task_id)
    if not task:
        raise HTTPException(404, "分析任务不存在")
    project = get_project(db, task.project_id, user)
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
    project = get_project(db, project_id, user)
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
