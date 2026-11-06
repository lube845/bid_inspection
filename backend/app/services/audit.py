from sqlalchemy.orm import Session

from app.models import AuditLog, User


def log_audit(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor: User | None = None,
    payload: dict | None = None,
    *,
    result: str = "success",
    source: str = "web",
    before: dict | None = None,
    after: dict | None = None,
    entity_name: str | None = None,
    ip: str | None = None,
) -> AuditLog:
    """写一条操作审计记录。

    audit_log 表已有「操作人 / 操作时间 / 操作类型 / 操作对象」四列，PRD 4.13.4 另需
    「操作结果 / 操作来源 / 变更前后信息」。为避免改动表结构（项目没有 Alembic），
    这些字段统一写进 payload JSON 列。

    本函数不提交事务，由调用方 commit，保证审计记录与业务变更同事务。
    """
    data = dict(payload or {})
    data.setdefault("result", result)
    data.setdefault("source", source)
    if before is not None:
        data["before"] = before
    if after is not None:
        data["after"] = after
    if entity_name is not None:
        data["entity_name"] = entity_name
    if ip is not None:
        data["ip"] = ip
    record = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor.id if actor else None,
        actor_name=actor.display_name or actor.username if actor else "system",
        payload=data,
    )
    db.add(record)
    return record
