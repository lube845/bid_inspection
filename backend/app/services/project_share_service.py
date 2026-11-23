"""项目分享：把项目授权给其他用户。

只有对项目拥有**管理**权限的人（创建者、被授予管理者、系统管理员）才能分享，
且不能把权限授予得比自己更高——否则「审批」级别的人就能顺手把自己提成管理员。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Project, ProjectMember, User
from app.models.enums import PROJECT_ROLE_LABELS, PROJECT_ROLE_RANK, ProjectRole
from app.services import access_service, audit


class ShareError(Exception):
    """分享相关的业务校验错误。"""


def _role_rank(role: str) -> int:
    return PROJECT_ROLE_RANK.get(role, 0)


def resolve_account(db: Session, account: str) -> User:
    """按工号或邮箱定位用户。

    与登录口径一致：用户名允许重复，不作为定位依据。
    """
    account = (account or "").strip()
    if not account:
        raise ShareError("请输入对方的工号或邮箱")
    user = db.scalars(select(User).where(User.employee_no == account)).first()
    if user is None:
        user = db.scalars(
            select(User).where(func.lower(User.email) == account.lower())
        ).first()
    if user is None:
        raise ShareError(f"没有找到账号为「{account}」的用户")
    if not user.is_active:
        raise ShareError(f"账号「{account}」已停用，不能分享")
    return user


def share_project(
    db: Session,
    project: Project,
    actor: User,
    account: str,
    role: str,
    *,
    commit: bool = True,
) -> dict:
    """把项目分享给一个用户；已分享过则更新其权限。"""
    if role not in {r.value for r in ProjectRole}:
        raise ShareError("权限级别不正确")
    actor_rank = _role_rank(access_service.primary_role(db, project, actor) or "")
    if _role_rank(role) > actor_rank:
        raise ShareError("不能授予高于自己权限的级别")

    target = resolve_account(db, account)
    if target.id == project.created_by:
        raise ShareError("对方是该项目的创建者，已拥有管理权限")
    if target.id == actor.id:
        raise ShareError("不能分享给自己")

    member = access_service.membership_for(db, project.id, target.id)
    if member is None:
        member = ProjectMember(
            project_id=project.id,
            user_id=target.id,
            role=role,
            granted_by=actor.id,
        )
        db.add(member)
        action, message = "project.share", "项目已分享"
    else:
        if member.role == role:
            raise ShareError("对方已拥有该权限")
        member.role = role
        member.granted_by = actor.id
        action, message = "project.share_update", "权限已更新"

    db.flush()
    audit.log_audit(
        db,
        action,
        "project",
        project.id,
        actor,
        {
            "name": project.project_name,
            "target_user_id": target.id,
            "target_account": target.employee_no or target.email,
            "role": role,
        },
        entity_name=project.project_name,
    )
    if commit:
        db.commit()
    return {
        "message": message,
        "member": _member_payload(db, member),
    }


def update_member_role(
    db: Session,
    project: Project,
    actor: User,
    member: ProjectMember,
    role: str,
    *,
    commit: bool = True,
) -> dict:
    if role not in {r.value for r in ProjectRole}:
        raise ShareError("权限级别不正确")
    if member.user_id == project.created_by:
        raise ShareError("创建者的管理权限不能修改")
    actor_rank = _role_rank(access_service.primary_role(db, project, actor) or "")
    if max(_role_rank(role), _role_rank(member.role)) > actor_rank:
        raise ShareError("不能修改高于自己权限的授权")

    before = member.role
    member.role = role
    member.granted_by = actor.id
    db.flush()
    audit.log_audit(
        db,
        "project.share_update",
        "project",
        project.id,
        actor,
        {"name": project.project_name, "role": role},
        before={"role": before},
        after={"role": role},
        entity_name=project.project_name,
    )
    if commit:
        db.commit()
    return {"message": "权限已更新", "member": _member_payload(db, member)}


def revoke_member(
    db: Session,
    project: Project,
    actor: User,
    member: ProjectMember,
    *,
    commit: bool = True,
) -> dict:
    if member.user_id == project.created_by:
        raise ShareError("不能移除项目创建者")
    actor_rank = _role_rank(access_service.primary_role(db, project, actor) or "")
    if _role_rank(member.role) > actor_rank:
        raise ShareError("不能移除高于自己权限的授权")

    # 先取出 user_id：db.delete 之后对象已标记删除，取属性会触发刷新
    target_user_id = member.user_id
    target = db.get(User, target_user_id)
    label = (
        (target.display_name or target.employee_no or target.username)
        if target
        else target_user_id
    )
    db.delete(member)
    db.flush()
    audit.log_audit(
        db,
        "project.share_revoke",
        "project",
        project.id,
        actor,
        {"name": project.project_name, "target_user_id": target_user_id},
        entity_name=project.project_name,
    )
    if commit:
        db.commit()
    return {"message": f"已移除「{label}」的访问权限"}


def _member_payload(db: Session, member: ProjectMember) -> dict:
    user = db.get(User, member.user_id)
    return {
        "id": member.id,
        "user_id": member.user_id,
        "username": user.username if user else "",
        "employee_no": user.employee_no if user else None,
        "email": user.email if user else None,
        "display_name": (
            (user.display_name or user.employee_no or user.username) if user else ""
        ),
        "role": member.role,
        "role_label": PROJECT_ROLE_LABELS.get(member.role, member.role),
        "is_owner": False,
        "granted_by": member.granted_by,
        "created_at": member.created_at,
    }
