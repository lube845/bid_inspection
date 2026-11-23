from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=255)
    project_code: str | None = Field(default=None, max_length=128)
    description: str | None = None


class ProjectUpdate(BaseModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=255)
    project_code: str | None = Field(default=None, max_length=128)
    description: str | None = None


class ProjectShareRequest(BaseModel):
    """把项目分享给某个用户：按工号或邮箱定位账号。"""

    # 工号或邮箱，二选一填写（服务层统一处理）
    account: str = Field(min_length=1, max_length=254)
    role: str = Field(default="viewer", pattern="^(viewer|reviewer|manager)$")


class ProjectMemberUpdate(BaseModel):
    role: str = Field(pattern="^(viewer|reviewer|manager)$")
