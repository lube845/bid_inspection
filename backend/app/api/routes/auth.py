from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.models import User
from app.schemas.common import LoginRequest, TokenResponse
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _authenticate(db: Session, username: str, password: str) -> User:
    user = db.scalars(select(User).where(User.username == username)).first()
    # 先校验密码，再暴露「已停用」状态，避免通过错误密码探测账号是否存在
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="账号已停用，请联系管理员")
    return user


def _user_payload(user: User, *, warn_default_password: bool = False) -> dict:
    payload = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
    }
    if warn_default_password:
        payload["warn_default_password"] = True
    return payload


def _token_response(user: User, password: str | None = None) -> TokenResponse:
    # 仍在使用部署配置里的初始口令时下发告警标记，前端引导立即修改密码。
    # 只比较口令本身，不落库明文。
    using_default = bool(
        password is not None
        and settings.admin_password
        and user.username == settings.admin_username
        and password == settings.admin_password
    )
    return TokenResponse(
        access_token=create_access_token(
            user.id,
            {"username": user.username, "role": user.role},
            token_version=user.token_version or 0,
        ),
        user=_user_payload(user, warn_default_password=using_default),
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = _authenticate(db, payload.username, payload.password)
    audit.log_audit(
        db,
        "auth.login",
        "app_user",
        user.id,
        user,
        {"username": user.username},
        entity_name=user.username,
    )
    db.commit()
    return _token_response(user, payload.password)


@router.post("/token", response_model=TokenResponse, include_in_schema=False)
def login_form(
    form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> TokenResponse:
    user = _authenticate(db, form.username, form.password)
    audit.log_audit(
        db,
        "auth.login",
        "app_user",
        user.id,
        user,
        {"username": user.username, "via": "form"},
        entity_name=user.username,
    )
    db.commit()
    return _token_response(user, form.password)


@router.post("/logout")
def logout(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """退出登录：递增令牌版本号，立即吊销该账号已签发的全部令牌与票据。

    无状态 JWT 无法逐条撤回，令牌版本号是等价的撤销机制——旧令牌里的
    ``tv`` 与数据库中的版本对不上，后续请求一律 401。
    """
    user.token_version = int(user.token_version or 0) + 1
    audit.log_audit(
        db,
        "auth.logout",
        "app_user",
        user.id,
        user,
        {"username": user.username, "token_version": user.token_version},
        entity_name=user.username,
    )
    db.commit()
    return {"message": "已退出登录"}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return _user_payload(user)
