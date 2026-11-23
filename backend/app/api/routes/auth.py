from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.models import User
from app.schemas.common import LoginRequest, PasswordChange, TokenResponse
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _authenticate(db: Session, account: str, password: str) -> User:
    """用工号或邮箱登录。

    用户名允许重复（同名同事），因此**不作为登录凭证**；工号与邮箱各自唯一，
    邮箱不区分大小写。两者都不匹配时统一报「账号或密码错误」，不提示是哪一项错。
    """
    account = (account or "").strip()
    user = None
    if account:
        user = db.scalars(select(User).where(User.employee_no == account)).first()
        if user is None:
            user = db.scalars(
                select(User).where(func.lower(User.email) == account.lower())
            ).first()
    # 先校验密码，再暴露「已停用」状态，避免通过错误密码探测账号是否存在
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="账号已停用，请联系管理员")
    return user


def _user_payload(user: User, *, warn_default_password: bool = False) -> dict:
    payload = {
        "id": user.id,
        "username": user.username,
        "employee_no": user.employee_no,
        "email": user.email,
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


@router.post("/change-password")
def change_password(
    payload: PasswordChange,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """用户自助修改密码。

    部署时下发的初始管理员口令只能由用户自己改掉——否则「首次登录必须改密」
    要么做不到（只能管理员代改），要么只能靠提示。改密后递增令牌版本号，
    旧令牌与票据立即失效，前端需重新登录。
    """
    from app.services import system_service

    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与原密码相同")
    if (
        user.username == settings.admin_username
        and payload.new_password == settings.admin_password
    ):
        raise HTTPException(status_code=400, detail="新密码不能继续使用部署配置的初始口令")
    system_service.reset_password(db, user, payload.new_password)
    audit.log_audit(
        db,
        "auth.change_password",
        "app_user",
        user.id,
        user,
        {"username": user.username},
        entity_name=user.username,
    )
    db.commit()
    return {"message": "密码已修改，请使用新密码重新登录"}
