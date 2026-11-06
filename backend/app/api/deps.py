from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
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


def get_current_user_allow_query(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None, description="预览 PDF 时通过查询参数传递令牌"),
    db: Session = Depends(get_db),
) -> User:
    value = token
    if not value and authorization and authorization.lower().startswith("bearer "):
        value = authorization[7:].strip()
    return _user_from_token(value, db)


def get_stream_user(
    token: str | None = Query(default=None, description="EventSource 通过查询参数传递令牌"),
) -> User:
    """SSE 认证：校验完立即释放数据库连接，避免长连接占用连接池。"""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少访问令牌")
    db = SessionLocal()
    try:
        return _user_from_token(token, db)
    finally:
        db.close()


def require_admin(user: User = Depends(get_current_user)) -> User:
    """系统管理写操作需要系统管理员权限。"""
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要系统管理员权限"
        )
    return user
