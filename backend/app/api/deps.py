from datetime import datetime, time, timezone

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import decode_access_token, decode_scoped_ticket
from app.db.session import SessionLocal, get_db
from app.models import Company, Project, User
from app.models.enums import UserRole
from app.services import access_service


def error_response(exc: Exception) -> HTTPException:
    """把服务层校验异常统一包成 400（各路由的 except 分支共用）。"""
    return HTTPException(status_code=400, detail=str(exc))


def parse_date_bound(value: str | None, end: bool = False):
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


def get_project(
    db: Session, project_id: str, user: User, role: str = access_service.VIEW
) -> Project:
    """按项目权限取项目；无权访问时统一转成 403/404，避免暴露项目是否存在。"""
    try:
        return access_service.ensure_access_by_id(db, project_id, user, role)
    except access_service.ProjectAccessError as exc:
        raise HTTPException(403 if exc.forbidden else 404, str(exc)) from exc


def get_company(
    db: Session, company_id: str, user: User, role: str = access_service.VIEW
) -> Company:
    """企业挂在项目下：权限一律以所属项目为准。"""
    company = db.get(Company, company_id)
    if not company or company.deleted_at:
        raise HTTPException(404, "企业不存在")
    get_project(db, company.project_id, user, role)
    return company


def _user_from_token(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)
    if (
        not payload
        or not payload.get("sub")
        # scope_ticket 只有限定用途，不能当作登录凭证使用
        or payload.get("typ") not in (None, "access")
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的登录凭证")
    user = db.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
    # 令牌版本号对不上说明用户已退出登录 / 重置密码 / 被禁用过：旧令牌立即作废
    if int(payload.get("tv") or 0) != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效，请重新登录"
        )
    return user


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return _user_from_token(token, db)


def get_file_access_user(
    file_id: str,
    ticket: str | None = Query(default=None, description="文件预览的短期访问票据"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """文件预览认证：优先用 Authorization，其次校验限定到该文件的短期票据。

    URL 中不再接受长期登录 JWT，避免令牌进入浏览器历史、Referer 与访问日志。
    """
    if authorization and authorization.lower().startswith("bearer "):
        return _user_from_token(authorization[7:].strip(), db)
    payload = decode_scoped_ticket(ticket, "file", file_id)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="访问票据无效或已过期"
        )
    return _load_active_user(db, payload["sub"], payload.get("tv"))


def get_stream_user(
    project_id: str,
    ticket: str | None = Query(default=None, description="SSE 的短期访问票据"),
) -> User:
    """SSE 认证：校验限定到项目的短期票据，校验完立即释放数据库连接。"""
    payload = decode_scoped_ticket(ticket, "events", project_id)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="访问票据无效或已过期"
        )
    db = SessionLocal()
    try:
        return _load_active_user(db, payload["sub"], payload.get("tv"))
    finally:
        db.close()


def _load_active_user(db: Session, subject: str, token_version=None) -> User:
    user = db.get(User, subject)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用"
        )
    if token_version is not None and int(token_version) != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="访问票据无效或已过期"
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """系统管理写操作需要系统管理员权限。"""
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要系统管理员权限"
        )
    return user
