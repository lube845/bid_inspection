"""系统管理：用户账号与操作日志。

只依赖既有表（app_user / audit_log），不新增字段：
- 用户管理复用 app_user 现有的 username / password_hash / display_name / role / is_active。
- 操作日志复用 audit_log；PRD 4.13.4 需要的「操作结果 / 操作来源 / 变更前后信息」
  写在 payload JSON 里（见 services/audit.py）。
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import AuditLog, User
from app.models.enums import USER_ROLE_LABELS, UserRole

# 操作类型的中文标签，对齐 PRD 4.13.4 列举的事件
ACTION_LABELS = {
    "auth.login": "登录系统",
    "auth.logout": "退出系统",
    "auth.change_password": "修改密码",
    "project.create": "创建项目",
    "project.update": "修改项目",
    "project.delete": "删除项目",
    "project.purge": "彻底删除项目",
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
    "price_quote.manual_entry": "补录报价",
    "price_quote.import": "导入报价",
    "price_quote.delete": "删除报价",
    "price_item.import": "导入清单单价",
    "price_item.manual_entry": "补录清单单价",
    "evidence.assign": "分派证据",
    "evidence.review_cluster": "按线索审核",
    "template_fragment.create": "标记模板片段",
    "template_fragment.revoke": "撤销模板片段",
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
    "price_quote": "投标报价",
    "price_item": "清单分项",
    "template_fragment": "模板片段",
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
        query = query.where(
            User.username.ilike(pattern)
            | User.display_name.ilike(pattern)
            | User.employee_no.ilike(pattern)
            | User.email.ilike(pattern)
        )
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
        "employee_no": user.employee_no,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "role_label": USER_ROLE_LABELS.get(user.role, user.role),
        "is_active": user.is_active,
        "created_at": user.created_at,
        "last_login_at": getattr(user, "last_login_at", None),
    }


# 邮箱只做轻量格式校验：项目不引入 email-validator 依赖，够挡住手滑写错即可
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_identity(
    db: Session,
    fields: dict,
    *,
    exclude_id: str | None = None,
) -> dict:
    """清洗并校验用户名 / 工号 / 邮箱。

    ``fields`` 只包含调用方**显式设置**的字段：值为 None 表示清空，
    键不存在表示不改。这样才能区分「没传」与「传空」。

    只有**登录凭证**（工号、邮箱）要求唯一；用户名是展示用的，允许重复（同名同事）。
    邮箱统一转小写存储，空值不参与冲突判断。
    """
    data: dict = {}
    if "username" in fields:
        username = (fields["username"] or "").strip()
        if not username:
            raise SystemError("用户名不能为空")
        data["username"] = username
    if "employee_no" in fields:
        data["employee_no"] = (fields["employee_no"] or "").strip() or None
    if "email" in fields:
        email = (fields["email"] or "").strip().lower() or None
        if email is not None and not _EMAIL_RE.match(email):
            raise SystemError("邮箱格式不正确")
        data["email"] = email

    # 工号与邮箱都能登录，必须**跨字段**互不重复：否则 A 的工号可能等于 B 的邮箱，
    # 登录时到底登进哪个账号就取决于查询顺序。
    for field, label in (("employee_no", "工号"), ("email", "邮箱")):
        value = data.get(field)
        if not value:
            continue
        query = select(User.id).where(
            (User.employee_no == value) | (func.lower(User.email) == value.lower())
        )
        if exclude_id:
            query = query.where(User.id != exclude_id)
        if db.scalar(query):
            raise SystemError(f"{label}「{value}」已被其他账号使用")
    return data


def _check_identity_required(employee_no: str | None, email: str | None) -> None:
    """工号与邮箱必须至少填一个。"""
    if not (employee_no or email):
        raise SystemError("工号与邮箱至少填写一项")


def create_user(
    db: Session,
    username: str,
    password: str,
    display_name: str,
    role: str,
    actor: User | None = None,
    *,
    employee_no: str | None = None,
    email: str | None = None,
) -> dict:
    identity = _normalize_identity(
        db, {"username": username, "employee_no": employee_no, "email": email}
    )
    _check_identity_required(identity.get("employee_no"), identity.get("email"))
    if role not in {r.value for r in UserRole}:
        raise SystemError("用户角色不正确")
    user = User(
        **identity,
        password_hash=hash_password(password),
        display_name=display_name.strip() or identity["username"],
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
    *,
    identity_set: dict | None = None,
) -> dict:
    if role is not None and role not in {r.value for r in UserRole}:
        raise SystemError("用户角色不正确")
    # 不允许把自己降级或禁用，避免把系统锁死
    if actor is not None and user.id == actor.id:
        if role is not None and role != UserRole.ADMIN.value:
            raise SystemError("不能修改自己的管理员角色")
        if is_active is False:
            raise SystemError("不能禁用自己的账号")
    # 不允许禁用或降级最后一个可用的管理员
    losing_admin = (is_active is False or (role is not None and role != UserRole.ADMIN.value))
    if losing_admin and user.role == UserRole.ADMIN.value:
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

    if identity_set:
        identity = _normalize_identity(db, identity_set, exclude_id=user.id)
        merged = {
            "employee_no": user.employee_no,
            "email": user.email,
            **identity,
        }
        _check_identity_required(merged["employee_no"], merged["email"])
        for field, value in identity.items():
            setattr(user, field, value)
    if display_name is not None:
        user.display_name = display_name.strip()
    if role is not None:
        user.role = role
    if is_active is not None:
        user.is_active = is_active
        # 禁用账号时吊销已签发的令牌：否则已登录的会话在令牌过期前仍可继续使用
        if is_active is False:
            user.token_version = int(user.token_version or 0) + 1
    db.flush()
    return user_payload(user)


def reset_password(db: Session, user: User, password: str) -> dict:
    user.password_hash = hash_password(password)
    # 重置密码后旧令牌必须失效，防止密码泄露后旧会话继续可用
    user.token_version = int(user.token_version or 0) + 1
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
