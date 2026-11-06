from datetime import datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models import User
from app.schemas.system import (
    PasswordReset,
    RuleVersionActivate,
    RuleVersionCreate,
    UserCreate,
    UserUpdate,
)
from app.services import audit, rule_service, system_service

router = APIRouter(prefix="/system", tags=["system"])


def _handle(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- 检测规则


@router.get("/rules")
def list_rules(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """当前生效规则 + 版本历史（分析人员也需要查看当前生效规则）。"""
    versions = rule_service.list_versions(db)
    active = {
        dimension: rule_service.version_payload(record)
        for dimension, record in (
            (dimension, rule_service.active_version(db, dimension))
            for dimension in rule_service.DIMENSION_PARAMS
        )
        if record is not None
    }
    return {
        "dimensions": [
            {
                "dimension": dimension,
                "label": rule_service.DIMENSION_LABELS.get(dimension, dimension),
                "active": active.get(dimension),
                "default_params": rule_service.default_params(dimension),
            }
            for dimension in rule_service.DIMENSION_PARAMS
        ],
        "versions": [rule_service.version_payload(record) for record in versions],
    }


@router.post("/rules/versions", status_code=201)
def create_rule_version(
    payload: RuleVersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    """新建规则版本。默认保存为待生效；立即生效需要同时给出生效确认。"""
    if payload.activate and not payload.confirmed:
        raise HTTPException(400, "检测规则变更需要生效确认")
    try:
        record = rule_service.create_version(
            db,
            payload.dimension,
            payload.params,
            payload.change_reason,
            actor=user,
            activate=payload.activate,
        )
    except rule_service.RuleError as exc:
        raise _handle(exc) from exc
    audit.log_audit(
        db,
        "rule.update",
        "detection_rule_version",
        record.id,
        user,
        {"dimension": record.dimension, "version_no": record.version_no},
        before=record.previous_params,
        after=record.params,
        entity_name=f"{record.dimension} v{record.version_no}",
    )
    db.commit()
    db.refresh(record)
    return rule_service.version_payload(record)


@router.post("/rules/versions/{version_id}/activate")
def activate_rule_version(
    version_id: str,
    payload: RuleVersionActivate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    """生效确认：把待生效版本置为生效，同维度旧版本转为历史版本。"""
    if not payload.confirmed:
        raise HTTPException(400, "检测规则变更需要生效确认")
    try:
        record = rule_service.activate(db, version_id, actor=user)
    except rule_service.RuleError as exc:
        raise _handle(exc) from exc
    audit.log_audit(
        db,
        "rule.update",
        "detection_rule_version",
        record.id,
        user,
        {"dimension": record.dimension, "version_no": record.version_no, "activated": True},
        before=record.previous_params,
        after=record.params,
        entity_name=f"{record.dimension} v{record.version_no}",
    )
    db.commit()
    db.refresh(record)
    return rule_service.version_payload(record)


# --------------------------------------------------------------------------- 操作日志


@router.get("/logs")
def list_logs(
    action: str | None = None,
    actor_id: str | None = None,
    entity_type: str | None = None,
    result: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    return system_service.list_logs(
        db,
        action=action,
        actor_id=actor_id,
        entity_type=entity_type,
        result=result,
        source=source,
        date_from=_parse_date(date_from, end=False),
        date_to=_parse_date(date_to, end=True),
        page=page,
        page_size=page_size,
    )


@router.get("/log-options")
def log_options(
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    return {
        "actions": [
            {"value": value, "label": label} for value, label in system_service.ACTION_LABELS.items()
        ],
        "results": [
            {"value": value, "label": label} for value, label in system_service.RESULT_LABELS.items()
        ],
        "sources": [
            {"value": value, "label": label} for value, label in system_service.SOURCE_LABELS.items()
        ],
        "actors": system_service.list_log_actors(db),
    }


# --------------------------------------------------------------------------- 用户管理


@router.get("/users")
def list_users(
    q: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    return system_service.list_users(db, q=q, role=role, is_active=is_active)


@router.post("/users", status_code=201)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    try:
        created = system_service.create_user(
            db,
            payload.username,
            payload.password,
            payload.display_name,
            payload.role,
            actor=user,
        )
    except system_service.SystemError as exc:
        raise _handle(exc) from exc
    audit.log_audit(
        db,
        "user.create",
        "app_user",
        created["id"],
        user,
        {"username": created["username"], "role": created["role"]},
        after={"username": created["username"], "role": created["role"]},
        entity_name=created["username"],
    )
    db.commit()
    return created


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    before = {"role": target.role, "is_active": target.is_active, "display_name": target.display_name}
    try:
        updated = system_service.update_user(
            db,
            target,
            display_name=payload.display_name,
            role=payload.role,
            is_active=payload.is_active,
            actor=user,
        )
    except system_service.SystemError as exc:
        raise _handle(exc) from exc
    audit.log_audit(
        db,
        "user.update",
        "app_user",
        target.id,
        user,
        {"username": target.username},
        before=before,
        after={"role": updated["role"], "is_active": updated["is_active"], "display_name": updated["display_name"]},
        entity_name=target.username,
    )
    db.commit()
    return updated


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: str,
    payload: PasswordReset,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    system_service.reset_password(db, target, payload.password)
    audit.log_audit(
        db,
        "user.reset_password",
        "app_user",
        target.id,
        user,
        {"username": target.username},
        entity_name=target.username,
    )
    db.commit()
    return {"message": "密码已重置"}


def _parse_date(value: str | None, end: bool = False):
    """把 YYYY-MM-DD 解析成 UTC 边界，避免 SQLite/Postgres 时区不一致。"""
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if end:
        parsed = datetime.combine(parsed.date(), time.max)
    return parsed.replace(tzinfo=timezone.utc)
