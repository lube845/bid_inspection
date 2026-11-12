from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def create_access_token(subject: str, extra: dict | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "exp": expire, **(extra or {})}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None


def create_scoped_ticket(
    subject: str,
    purpose: str,
    resource: str,
    ttl_seconds: int,
) -> str:
    """签发短期、限定用途与资源的一次性访问票据。

    用于浏览器原生能力（<a href>、<iframe>、EventSource）无法携带
    Authorization 请求头的场景，避免把长期登录 JWT 放进 URL/历史记录。
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_seconds)))
    payload = {
        "sub": subject,
        "purpose": purpose,
        "resource": resource,
        "exp": expire,
        "typ": "scope_ticket",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_scoped_ticket(
    token: str | None, purpose: str, resource: str
) -> str | None:
    """校验票据的用途与资源绑定，成功时返回用户 id。"""
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    if payload.get("typ") != "scope_ticket":
        return None
    if payload.get("purpose") != purpose or payload.get("resource") != resource:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None
