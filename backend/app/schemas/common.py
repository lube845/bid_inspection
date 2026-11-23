from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # 用户名、工号、邮箱都可以登录，字段名沿用 username 以兼容旧客户端
    username: str
    password: str


class PasswordChange(BaseModel):
    """用户自助修改密码：必须提供原密码。"""

    current_password: str
    new_password: str = Field(min_length=8, max_length=64)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict
