from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=255)
    project_code: str | None = Field(default=None, max_length=128)
    description: str | None = None


class ProjectUpdate(BaseModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=255)
    project_code: str | None = Field(default=None, max_length=128)
    description: str | None = None
    # 结论需要两人复核时才终审；只影响之后发起的审核，不追溯已终审证据
    requires_second_review: bool | None = None
