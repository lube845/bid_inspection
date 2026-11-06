"""系统管理：用户账号与操作日志。

只依赖既有表（app_user / audit_log），不新增字段：
- 用户管理复用 app_user 现有的 username / password_hash / display_name / role / is_active。
- 操作日志复用 audit_log；PRD 4.13.4 需要的「操作结果 / 操作来源 / 变更前后信息」
  写在 payload JSON 里（见 services/audit.py）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import AuditLog, User
from app.models.enums import UserRole

# 操作类型的中文标签，对齐 PRD 4.13.4 列举的事件
ACTION_LABELS = {
    "auth.login": "登录系统",
    "auth.logout": "退出系统",
    "project.create": "创建项目",
    "project.update": "修改项目",
    "project.delete": "删除项目",
    "project.archive": "归档项目",
    "project.unarchive": "取消归档",
    "company.create": "创建企业",
    "company.update": "修改企业",
    "company.delete": "删除企业",
    "company.confirm": "确认企业资料",
    "file.upload": "上传文件",
    "file.download": "下载文件",
    "file.replace": "替换文件",
    "file.delete": "删除文件",
    "file.parse": "解析文件",
    "file.parse_failed": "文件解析失败",
    "analysis.create": "启动分析",
    "analysis.cancel": "取消分析",
    "analysis.finish": "分析结束",
    "analysis.retry_pair": "重试任务",
    "evidence.review": "审核证据",
    "report.export": "导出报告",
    "rule.update": "修改检测规则",
    "user.create": "新增用户",
    "user.update": "修改用户",
    "user.reset_password": "重置密码",
}

RESULT_LABELS = {"success": "成功", "failed": "失败"}
SOURCE_LABELS = {"web": "网页端", "api": "接口调用", "system": "系统内部"}

ENTITY_LABELS = {
    "project": "项目",
    "original_file": "文件",
    "analysis_task": "分析任务",
    "comparison_evidence": "证据",
    "app_user": "用户",
    "detection_rule_version": "检测规则",
    "company": "投标企业",
}


class SystemError(Exception):
    """系统管理相关的业务校验错误。"""


# --------------------------------------------------------------------------- 用户


def list_users(
    db: Session,
    q: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
) -> dict:
    query = select(User)
    if q:
        pattern = f"%{q}%"
        query = query.where(User.username.ilike(pattern) | User.display_name.ilike(pattern))
    if role:
        query = query.where(User.role == role)
    if is_active is not None:
        query = query.where(User.is_active.is_(is_active))
    rows = list(db.scalars(query.order_by(User.created_at.desc())))
    return {
        "items": [user_payload(user) for user in rows],
        "total": len(rows),
    }


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "role_label": {
            UserRole.ADMIN.value: "系统管理员",
            UserRole.ANALYST.value: "项目分析人员",
            UserRole.REVIEWER.value: "风险审核人员",
        }.get(user.role, user.role),
        "is_active": user.is_active,
        "created_at": user.created_at,
        "last_login_at": getattr(user, "last_login_at", None),
    }


def create_user(
    db: Session,
    username: str,
    password: str,
    display_name: str,
    role: str,
    actor: User | None = None,
) -> dict:
    username = username.strip()
    if db.scalar(select(User).where(User.username == username)):
        raise SystemError(f"用户名 {username} 已存在")
    if role not in {r.value for r in UserRole}:
        raise SystemError("用户角色不正确")
    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name.strip() or username,
        role=role,
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user_payload(user)


def update_user(
    db: Session,
    user: User,
    display_name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    actor: User | None = None,
) -> dict:
    if role is not None and role not in {r.value for r in UserRole}:
        raise SystemError("用户角色不正确")
    # 不允许把自己降级或禁用，避免把系统锁死
    if actor is not None and user.id == actor.id:
        if role is not None and role != UserRole.ADMIN.value:
            raise SystemError("不能修改自己的管理员角色")
        if is_active is False:
            raise SystemError("不能禁用自己的账号")
    # 不允许禁用最后一个可用的管理员
    if is_active is False and user.role == UserRole.ADMIN.value:
        remaining = db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.role == UserRole.ADMIN.value,
                User.is_active.is_(True),
                User.id != user.id,
            )
        )
        if not remaining:
            raise SystemError("系统至少需要保留一个启用的管理员账号")

    if display_name is not None:
        user.display_name = display_name.strip()
    if role is not None:
        user.role = role
    if is_active is not None:
        user.is_active = is_active
    db.flush()
    return user_payload(user)


def reset_password(db: Session, user: User, password: str) -> dict:
    user.password_hash = hash_password(password)
    db.flush()
    return user_payload(user)


# --------------------------------------------------------------------------- 操作日志


def list_logs(
    db: Session,
    action: str | None = None,
    actor_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    result: str | None = None,
    source: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    def build_query(json_filters: bool):
        query = select(AuditLog)
        if action:
            query = query.where(AuditLog.action == action)
        if actor_id:
            query = query.where(AuditLog.actor_id == actor_id)
        if entity_type:
            query = query.where(AuditLog.entity_type == entity_type)
        if entity_id:
            query = query.where(AuditLog.entity_id == entity_id)
        if date_from:
            query = query.where(AuditLog.created_at >= date_from)
        if date_to:
            query = query.where(AuditLog.created_at <= date_to)
        # 「操作结果 / 操作来源」写在 payload JSON 里，用 JSON 提取在库内过滤，
        # 保证 total 与分页反映筛选条件。
        if json_filters:
            # 历史日志的 payload 可能没有这两个键，展示时默认 success / web，
            # 过滤时用 COALESCE 保持一致。
            if result:
                query = query.where(
                    func.coalesce(AuditLog.payload["result"].as_string(), "success") == result
                )
            if source:
                query = query.where(
                    func.coalesce(AuditLog.payload["source"].as_string(), "web") == source
                )
        return query

    try:
        query = build_query(True)
        total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = list(
            db.scalars(
                query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        items = [log_payload(record) for record in rows]
    except Exception:
        # 数据库不支持 JSON 提取（如缺少 JSON1 扩展）时退回内存过滤
        all_rows = list(
            db.scalars(
                build_query(False).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            )
        )
        if result:
            all_rows = [
                record for record in all_rows
                if (record.payload or {}).get("result", "success") == result
            ]
        if source:
            all_rows = [
                record for record in all_rows
                if (record.payload or {}).get("source", "web") == source
            ]
        total = len(all_rows)
        start = (page - 1) * page_size
        items = [log_payload(record) for record in all_rows[start : start + page_size]]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


def log_payload(record: AuditLog) -> dict:
    payload = record.payload or {}
    result = payload.get("result") or "success"
    source = payload.get("source") or "web"
    return {
        "id": record.id,
        "actor_id": record.actor_id,
        "actor_name": record.actor_name,
        "action": record.action,
        "action_label": ACTION_LABELS.get(record.action, record.action),
        "entity_type": record.entity_type,
        "entity_label": ENTITY_LABELS.get(record.entity_type, record.entity_type),
        "entity_id": record.entity_id,
        "entity_name": payload.get("entity_name"),
        "result": result,
        "result_label": RESULT_LABELS.get(result, result),
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "before": payload.get("before"),
        "after": payload.get("after"),
        "message": payload.get("message"),
        "created_at": record.created_at,
    }


def list_log_actors(db: Session) -> list[dict]:
    """出现过操作记录的人员（用于筛选下拉）。"""
    rows = db.execute(
        select(AuditLog.actor_id, AuditLog.actor_name)
        .where(AuditLog.actor_id.isnot(None))
        .group_by(AuditLog.actor_id, AuditLog.actor_name)
    ).all()
    return [{"id": actor_id, "name": name} for actor_id, name in rows if actor_id]
