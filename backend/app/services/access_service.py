"""项目级访问控制.

权限模型很简单，但调用点很多，因此集中在这里，接口层只负责取 project 再问一句
「这个人能不能做这件事」：

- **创建者**：始终对项目拥有管理权限；
- **系统管理员**：对所有项目拥有管理权限（不需要逐条授权）；
- **被分享的用户**：按 ``project_member.role`` 得到查看 / 审批 / 管理之一；
- **其他人**：完全看不到这个项目——列表不返回、详情 404，与项目不存在同样处理，
  避免通过 404/403 的差异探测项目是否存在。

权限是叠加的：管理 ⊃ 审批 ⊃ 查看。
"""

from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import Project, ProjectMember, User
from app.models.enums import PROJECT_ROLE_RANK, ProjectRole, UserRole

# 便捷别名，避免调用点写字符串
VIEW = ProjectRole.VIEWER.value
REVIEW = ProjectRole.REVIEWER.value
MANAGE = ProjectRole.MANAGER.value


class ProjectAccessError(Exception):
    """无权访问项目。接口层转成 404/403。"""

    def __init__(self, message: str = "项目不存在或无权访问", *, forbidden: bool = False):
        super().__init__(message)
        self.forbidden = forbidden


def is_system_admin(user: User | None) -> bool:
    return bool(user and user.role == UserRole.ADMIN.value)


def memberships(db: Session, user_id: str) -> dict[str, str]:
    """该用户被分享到的项目 → 权限级别。"""
    return dict(
        db.execute(
            select(ProjectMember.project_id, ProjectMember.role).where(
                ProjectMember.user_id == user_id
            )
        ).all()
    )


def allowed_roles(db: Session, project: Project, user: User | None) -> set[str]:
    """用户在某个项目上拥有的权限级别集合。空集合表示完全不可见。"""
    if user is None:
        return set()
    if is_system_admin(user) or project.created_by == user.id:
        # 管理员与创建者拥有全部权限
        return {VIEW, REVIEW, MANAGE}
    row = db.scalars(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user.id,
        )
    ).first()
    if row is None:
        return set()
    # 高级权限包含低级：管理员可以分享为「审批」以外的任意级别，这里按等级展开
    rank = PROJECT_ROLE_RANK.get(row.role, 0)
    return {role for role, level in PROJECT_ROLE_RANK.items() if level <= rank}


def has_role(db: Session, project: Project, user: User | None, role: str) -> bool:
    return role in allowed_roles(db, project, user)


def primary_role(db: Session, project: Project, user: User | None) -> str | None:
    """用户在项目上的最高权限级别，用于前端显示与按钮控制。"""
    roles = allowed_roles(db, project, user)
    if not roles:
        return None
    return max(roles, key=lambda role: PROJECT_ROLE_RANK[role])


def visible_project_ids(db: Session, user: User | None) -> list[str] | None:
    """用户可见的项目 id 列表。

    返回 ``None`` 表示「不做项目级过滤」（系统管理员）——这样列表、工作台、
    风险统计可以直接跳过过滤，避免把全部 id 塞进 SQL 的 IN 子句。
    """
    if user is None:
        return []
    if is_system_admin(user):
        return None
    shared = db.scalars(
        select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    ).all()
    owned = db.scalars(select(Project.id).where(Project.created_by == user.id)).all()
    return list({*owned, *shared})


def ensure_access(
    db: Session,
    project: Project | None,
    user: User | None,
    role: str = VIEW,
) -> Project:
    """校验访问权限，不通过时抛 ``ProjectAccessError``。

    - 项目不存在 / 已删除 / 对当前用户不可见：``forbidden=False`` → 接口按 404 处理；
    - 项目可见但权限不足：``forbidden=True`` → 接口按 403 处理。
    """
    if project is None or project.deleted_at is not None:
        raise ProjectAccessError()
    roles = allowed_roles(db, project, user)
    if not roles:
        raise ProjectAccessError()
    if role not in roles:
        raise ProjectAccessError("当前账号在该项目上没有此操作权限", forbidden=True)
    return project


def ensure_access_by_id(
    db: Session,
    project_id: str,
    user: User | None,
    role: str = VIEW,
) -> Project:
    return ensure_access(db, db.get(Project, project_id), user, role)


def share_targets(db: Session, user: User | None) -> list[dict]:
    """可分享的用户名单：全部启用账号，排除自己。

    只返回工号/邮箱与显示名——这是给「分享」弹窗按工号或邮箱查找用的，
    不暴露密码状态等敏感信息。
    """
    query = select(User).where(User.is_active.is_(True))
    if user is not None:
        query = query.where(User.id != user.id)
    rows = db.scalars(query.order_by(User.display_name, User.username))
    return [
        {
            "id": row.id,
            "username": row.username,
            "employee_no": row.employee_no,
            "email": row.email,
            "display_name": row.display_name or row.employee_no or row.username,
        }
        for row in rows
    ]


def member_payload(
    db: Session,
    project: Project,
    *,
    include_owner: bool = True,
) -> dict:
    """项目成员列表：创建者 + 被分享的用户。"""
    owner = db.get(User, project.created_by) if project.created_by else None
    rows = list(
        db.scalars(
            select(ProjectMember)
            .where(ProjectMember.project_id == project.id)
            .order_by(ProjectMember.created_at)
        )
    )
    user_ids = {row.user_id for row in rows}
    if owner is not None:
        user_ids.add(owner.id)
    users = {
        row.id: row
        for row in db.scalars(select(User).where(User.id.in_(user_ids)))
    } if user_ids else {}

    items: list[dict] = []
    if include_owner and owner is not None:
        items.append(
            {
                # 创建者没有 project_member 行：id 为空，前端据此隐藏「移除/改权限」
                "id": None,
                "user_id": owner.id,
                "username": owner.username,
                "employee_no": owner.employee_no,
                "email": owner.email,
                "display_name": owner.display_name or owner.employee_no or owner.username,
                # 创建者固定拥有管理权限，且不能被移除或降级
                "role": MANAGE,
                "role_label": "管理",
                "is_owner": True,
                "granted_by": None,
                "created_at": project.created_at,
            }
        )
    for row in rows:
        target = users.get(row.user_id)
        if target is None:
            # 用户已被删除：跳过，避免列表出现空行
            continue
        items.append(
            {
                "id": row.id,
                "user_id": target.id,
                "username": target.username,
                "employee_no": target.employee_no,
                "email": target.email,
                "display_name": target.display_name
                or target.employee_no
                or target.username,
                "role": row.role,
                "role_label": {"viewer": "查看", "reviewer": "审批", "manager": "管理"}.get(
                    row.role, row.role
                ),
                "is_owner": False,
                "granted_by": row.granted_by,
                "created_at": row.created_at,
            }
        )
    return {"items": items, "total": len(items), "owner_id": project.created_by}


def visible_projects_query(db: Session, user: User | None):
    """项目查询的基础过滤：未删除 + 当前用户可见。"""
    query = select(Project).where(Project.deleted_at.is_(None))
    ids = visible_project_ids(db, user)
    if ids is not None:
        query = query.where(Project.id.in_(ids))
    return query


def membership_for(db: Session, project_id: str, user_id: str) -> ProjectMember | None:
    return db.scalars(
        select(ProjectMember).where(
            and_(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).first()
