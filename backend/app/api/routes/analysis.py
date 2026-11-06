from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import AnalysisTask, ComparisonPair, Project, User
from app.schemas.analysis import StartAnalysisRequest
from app.services import analysis_service, audit, results_service
from app.workers import dispatch

router = APIRouter(tags=["analysis"])


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    return project


def _get_task(db: Session, task_id: str) -> AnalysisTask:
    task = db.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(404, "分析任务不存在")
    return task


@router.get("/projects/{project_id}/analysis/prepare")
def prepare_analysis(
    project_id: str,
    company_ids: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    return analysis_service.prepare(db, project, company_ids)


@router.post("/projects/{project_id}/analysis/tasks", status_code=201)
def start_analysis(
    project_id: str,
    payload: StartAnalysisRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    task = analysis_service.create_task(db, project, payload, user)
    dispatch.enqueue_analysis(task.id)
    db.expire_all()
    task = _get_task(db, task.id)
    return analysis_service.task_detail(db, task)


@router.get("/projects/{project_id}/tasks")
def list_tasks(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    return {"items": results_service.task_history(db, project.id)}


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    task = _get_task(db, task_id)
    return analysis_service.task_detail(db, task)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    task = _get_task(db, task_id)
    task = analysis_service.cancel_task(db, task, user)
    return analysis_service.task_detail(db, task)


@router.post("/tasks/{task_id}/pairs/{pair_id}/retry")
def retry_pair(
    task_id: str,
    pair_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    task = _get_task(db, task_id)
    pair = db.get(ComparisonPair, pair_id)
    if not pair or pair.analysis_task_id != task.id:
        raise HTTPException(404, "企业组合不存在")
    audit.log_audit(
        db,
        "analysis.retry_pair",
        "analysis_task",
        task.id,
        user,
        {"pair_id": pair.id, "task_no": task.task_no},
        entity_name=f"任务 #{task.task_no}",
    )
    db.commit()
    dispatch.enqueue_retry_pair(task.id, pair.id)
    db.expire_all()
    task = _get_task(db, task.id)
    return analysis_service.task_detail(db, task)
