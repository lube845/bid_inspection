from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import AnalysisTask, Company, OriginalFile, Project, ProjectMember, User
from app.models.base import utcnow
from app.models.enums import (
    PROJECT_STATUS_LABELS,
    FileKind,
    ProjectStatus,
    TaskStatus,
)
from app.schemas.project import (
    ProjectCreate,
    ProjectMemberUpdate,
    ProjectShareRequest,
    ProjectUpdate,
)
from app.services import (
    access_service,
    audit,
    project_service,
    project_share_service,
    results_service,
)

router = APIRouter(prefix="/projects", tags=["projects"])


def get_project_for(
    db: Session, project_id: str, user: User, role: str = access_service.VIEW
) -> Project:
    """取项目并校验权限；无权时 404（不可见）或 403（可见但权限不足）。"""
    try:
        return access_service.ensure_access_by_id(db, project_id, user, role)
    except access_service.ProjectAccessError as exc:
        raise HTTPException(
            403 if exc.forbidden else 404, str(exc)
        ) from exc


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
        "status_label": PROJECT_STATUS_LABELS.get(project.status, project.status),
        "created_by": project.created_by,
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
    query = access_service.visible_projects_query(db, user)
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

    # 一次性取出当前用户在各项目的权限级别，避免逐行查询
    shared_roles = (
        {}
        if access_service.is_system_admin(user)
        else access_service.memberships(db, user.id)
    )
    items = []
    for project in projects:
        payload = _project_payload(project)
        if access_service.is_system_admin(user) or project.created_by == user.id:
            my_role = access_service.MANAGE
        else:
            my_role = shared_roles.get(project.id)
        payload.update(
            {
                "company_count": company_counts.get(project.id, 0),
                "file_count": file_counts.get(project.id, 0),
                "last_analysis_at": last_analysis.get(project.id),
                "my_role": my_role,
                "is_owner": project.created_by == user.id,
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
    project = get_project_for(db, project_id, user)
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
    project = get_project_for(db, project_id, user, access_service.MANAGE)
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
    purge: bool = Query(default=False, description="彻底删除：连同文件、证据与任务记录一并物理清除"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """删除项目。

    默认是软删除：项目从列表与统计中消失，但原始文件与历史分析仍保留在库里，
    便于误删恢复与事后追溯。

    ``purge=true`` 时执行物理删除：磁盘文件、页面与文本块、证据与审核记录、
    分析任务、企业、报价、项目级模板片段一起清除，**不可恢复**。审计日志保留
    删除动作本身，用于回答「谁在什么时候彻底删除了哪个项目」。
    """
    project = get_project_for(db, project_id, user, access_service.MANAGE)
    if _has_running_task(db, project.id):
        raise HTTPException(400, "项目有分析任务正在执行，不能删除")
    if not purge:
        project.deleted_at = utcnow()
        project.status = ProjectStatus.ARCHIVED.value
        audit.log_audit(
            db,
            "project.delete",
            "project",
            project.id,
            user,
            {"name": project.project_name, "mode": "soft"},
        )
        db.commit()
        return {"message": "项目已删除（原始资料仍保留在数据库中）", "purged": False}

    stats = project_service.purge_project(db, project)
    audit.log_audit(
        db,
        "project.purge",
        "project",
        project_id,
        user,
        {"name": project.project_name, **stats},
        entity_name=project.project_name,
    )
    db.commit()
    return {
        "message": (
            f"项目已彻底删除：{stats['files']} 份文件、{stats['tasks']} 个分析任务、"
            f"{stats['evidence']} 条证据已一并清除"
        ),
        "purged": True,
        **stats,
    }


@router.post("/{project_id}/archive")
def archive_project(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = get_project_for(db, project_id, user, access_service.MANAGE)
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
    project = get_project_for(db, project_id, user, access_service.MANAGE)
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
    project = get_project_for(db, project_id, user)
    stats = project_service.project_stats(db, project)
    return {
        "project": _project_payload(project),
        "stats": stats,
        "recent_tasks": results_service.task_history(db, project.id)[:5],
    }


# --------------------------------------------------------------------------- 项目权限


@router.get("/{project_id}/permissions")
def project_permissions(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """项目成员与我的权限：权限页与详情页的按钮控制都读这里。"""
    project = get_project_for(db, project_id, user)
    payload = access_service.member_payload(db, project)
    payload["my_role"] = access_service.primary_role(db, project, user)
    payload["can_manage"] = access_service.has_role(
        db, project, user, access_service.MANAGE
    )
    payload["can_review"] = access_service.has_role(
        db, project, user, access_service.REVIEW
    )
    payload["is_owner"] = project.created_by == user.id
    payload["is_system_admin"] = access_service.is_system_admin(user)
    payload["share_targets"] = (
        access_service.share_targets(db, user) if payload["can_manage"] else []
    )
    return payload


@router.post("/{project_id}/permissions", status_code=201)
def share_project(
    project_id: str,
    payload: ProjectShareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """按工号或邮箱把项目分享给其他用户，并授予查看 / 审批 / 管理权限。"""
    project = get_project_for(db, project_id, user, access_service.MANAGE)
    try:
        return project_share_service.share_project(
            db, project, user, payload.account, payload.role
        )
    except project_share_service.ShareError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/{project_id}/permissions/{member_id}")
def update_project_member(
    project_id: str,
    member_id: str,
    payload: ProjectMemberUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = get_project_for(db, project_id, user, access_service.MANAGE)
    member = db.get(ProjectMember, member_id)
    if member is None or member.project_id != project.id:
        raise HTTPException(404, "该用户不在项目成员中")
    try:
        return project_share_service.update_member_role(
            db, project, user, member, payload.role
        )
    except project_share_service.ShareError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{project_id}/permissions/{member_id}")
def revoke_project_member(
    project_id: str,
    member_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = get_project_for(db, project_id, user, access_service.MANAGE)
    member = db.get(ProjectMember, member_id)
    if member is None or member.project_id != project.id:
        raise HTTPException(404, "该用户不在项目成员中")
    try:
        return project_share_service.revoke_member(db, project, user, member)
    except project_share_service.ShareError as exc:
        raise HTTPException(400, str(exc)) from exc
