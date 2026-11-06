from pydantic import BaseModel, Field


class CompanyCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    credit_code: str | None = Field(default=None, max_length=64)
    remark: str | None = None


class CompanyUpdate(BaseModel):
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    credit_code: str | None = Field(default=None, max_length=64)
    remark: str | None = None
    participate_analysis: bool | None = None


class FileOrderUpdate(BaseModel):
    file_ids: list[str]
