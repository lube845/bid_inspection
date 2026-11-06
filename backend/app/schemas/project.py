from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=255)
    project_code: str | None = Field(default=None, max_length=128)
    description: str | None = None


class ProjectUpdate(BaseModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=255)
    project_code: str | None = Field(default=None, max_length=128)
    description: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_name: str
    project_code: str | None
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    company_count: int = 0
    file_count: int = 0
    last_analysis_at: datetime | None = None
