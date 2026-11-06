from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.models import User
from app.schemas.common import LoginRequest, TokenResponse
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _authenticate(db: Session, username: str, password: str) -> User:
    user = db.scalars(select(User).where(User.username == username)).first()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return user


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
    }


def _token_response(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, {"username": user.username, "role": user.role}),
        user=_user_payload(user),
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
    return _token_response(user)


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
    return _token_response(user)


@router.post("/logout")
def logout(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """记录退出登录。前端在清除令牌前调用。"""
    audit.log_audit(
        db,
        "auth.logout",
        "app_user",
        user.id,
        user,
        {"username": user.username},
        entity_name=user.username,
    )
    db.commit()
    return {"message": "已退出登录"}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return _user_payload(user)
