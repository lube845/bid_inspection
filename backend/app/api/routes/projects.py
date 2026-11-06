from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import AnalysisTask, Company, OriginalFile, Project, User
from app.models.base import utcnow
from app.models.enums import FileKind, ProjectStatus, TaskStatus
from app.schemas.project import ProjectCreate, ProjectUpdate
from app.services import audit, project_service, results_service

router = APIRouter(prefix="/projects", tags=["projects"])


def _has_running_task(db: Session, project_id: str) -> bool:
    return (
        db.scalar(
            select(AnalysisTask.id)
            .where(
                AnalysisTask.project_id == project_id,
                AnalysisTask.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
            )
            .limit(1)
        )
        is not None
    )


def _project_payload(project: Project, stats: dict | None = None) -> dict:
    payload = {
        "id": project.id,
        "project_name": project.project_name,
        "project_code": project.project_code,
        "description": project.description,
        "status": project.status,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }
    if stats:
        payload["stats"] = stats
    return payload


@router.get("")
def list_projects(
    q: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    query = select(Project).where(Project.deleted_at.is_(None))
    if status:
        query = query.where(Project.status == status)
    elif not include_archived:
        query = query.where(Project.status != ProjectStatus.ARCHIVED.value)
    if q:
        query = query.where(
            Project.project_name.ilike(f"%{q}%") | Project.project_code.ilike(f"%{q}%")
        )

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    projects = list(
        db.scalars(
            query.order_by(Project.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    ids = [p.id for p in projects]

    company_counts = dict(
        db.execute(
            select(Company.project_id, func.count())
            .where(Company.project_id.in_(ids), Company.deleted_at.is_(None))
            .group_by(Company.project_id)
        ).all()
    ) if ids else {}
    file_counts = dict(
        db.execute(
            select(OriginalFile.project_id, func.count())
            .where(
                OriginalFile.project_id.in_(ids),
                OriginalFile.kind == FileKind.BID.value,
                OriginalFile.is_active.is_(True),
            )
            .group_by(OriginalFile.project_id)
        ).all()
    ) if ids else {}
    last_analysis = dict(
        db.execute(
            select(AnalysisTask.project_id, func.max(AnalysisTask.created_at))
            .where(AnalysisTask.project_id.in_(ids))
            .group_by(AnalysisTask.project_id)
        ).all()
    ) if ids else {}

    items = []
    for project in projects:
        payload = _project_payload(project)
        payload.update(
            {
                "company_count": company_counts.get(project.id, 0),
                "file_count": file_counts.get(project.id, 0),
                "last_analysis_at": last_analysis.get(project.id),
            }
        )
        items.append(payload)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("", status_code=201)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = Project(
        project_name=payload.project_name.strip(),
        project_code=payload.project_code,
        description=payload.description,
        status=ProjectStatus.PENDING.value,
        created_by=user.id,
    )
    db.add(project)
    db.flush()
    audit.log_audit(db, "project.create", "project", project.id, user, {"name": project.project_name})
    db.commit()
    db.refresh(project)
    return _project_payload(project, project_service.project_stats(db, project))


@router.get("/{project_id}")
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    stats = project_service.project_stats(db, project)
    payload = _project_payload(project, stats)
    payload["recent_tasks"] = results_service.task_history(db, project.id)[:5]
    return payload


@router.patch("/{project_id}")
def update_project(
    project_id: str,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    data = payload.model_dump(exclude_unset=True)
    if "project_name" in data and data["project_name"] is not None:
        data["project_name"] = data["project_name"].strip()
        if not data["project_name"]:
            raise HTTPException(400, "项目名称不能为空")
    for key, value in data.items():
        setattr(project, key, value)
    audit.log_audit(db, "project.update", "project", project.id, user, data)
    db.commit()
    db.refresh(project)
    return _project_payload(project, project_service.project_stats(db, project))


@router.delete("/{project_id}")
def delete_project(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    if _has_running_task(db, project.id):
        raise HTTPException(400, "项目有分析任务正在执行，不能删除")
    project.deleted_at = utcnow()
    project.status = ProjectStatus.ARCHIVED.value
    audit.log_audit(db, "project.delete", "project", project.id, user, {"name": project.project_name})
    db.commit()
    return {"message": "项目已删除"}


@router.post("/{project_id}/archive")
def archive_project(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    if _has_running_task(db, project.id):
        raise HTTPException(400, "项目有分析任务正在执行，不能归档")
    project.status = ProjectStatus.ARCHIVED.value
    audit.log_audit(db, "project.archive", "project", project.id, user)
    db.commit()
    return _project_payload(project)


@router.post("/{project_id}/unarchive")
def unarchive_project(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    project.status = ProjectStatus.PENDING.value
    project_service.refresh_project_status(db, project)
    audit.log_audit(db, "project.unarchive", "project", project.id, user)
    db.commit()
    return _project_payload(project, project_service.project_stats(db, project))


@router.get("/{project_id}/dashboard")
def project_dashboard(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    stats = project_service.project_stats(db, project)
    return {
        "project": _project_payload(project),
        "stats": stats,
        "recent_tasks": results_service.task_history(db, project.id)[:5],
    }
