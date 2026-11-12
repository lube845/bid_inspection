from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import decode_access_token, decode_scoped_ticket
from app.db.session import SessionLocal, get_db
from app.models import User
from app.models.enums import UserRole


def _user_from_token(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的登录凭证")
    user = db.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
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
    subject = decode_scoped_ticket(ticket, "file", file_id)
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="访问票据无效或已过期"
        )
    return _load_active_user(db, subject)


def get_stream_user(
    project_id: str,
    ticket: str | None = Query(default=None, description="SSE 的短期访问票据"),
) -> User:
    """SSE 认证：校验限定到项目的短期票据，校验完立即释放数据库连接。"""
    subject = decode_scoped_ticket(ticket, "events", project_id)
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="访问票据无效或已过期"
        )
    db = SessionLocal()
    try:
        user = db.get(User, subject)
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用"
            )
        return user
    finally:
        db.close()


def _load_active_user(db: Session, subject: str) -> User:
    user = db.get(User, subject)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用"
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """系统管理写操作需要系统管理员权限。"""
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要系统管理员权限"
        )
    return user
