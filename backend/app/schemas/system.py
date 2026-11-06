from pydantic import BaseModel, Field


class RuleVersionCreate(BaseModel):
    """新建检测规则版本。"""

    dimension: str = Field(pattern="^(text|personnel|price)$")
    params: dict = Field(default_factory=dict)
    change_reason: str = Field(min_length=1, max_length=500)
    # 立即生效需要同时给出 activate 与 confirmed（生效确认）
    activate: bool = False
    confirmed: bool = False


class RuleVersionActivate(BaseModel):
    """生效确认。"""

    confirmed: bool = False


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=4, max_length=64)
    display_name: str = Field(default="", max_length=128)
    role: str = Field(default="analyst", pattern="^(admin|analyst|reviewer)$")


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    role: str | None = Field(default=None, pattern="^(admin|analyst|reviewer)$")
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str = Field(min_length=4, max_length=64)
