from pydantic import BaseModel, Field

CreditCodeType = Field(default="uscc", pattern="^(uscc|other)$")


class CompanyCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    credit_code: str | None = Field(default=None, max_length=64)
    # uscc：统一社会信用代码，校验 18 位格式与校验位；other：其他证件，不校验格式
    credit_code_type: str = CreditCodeType
    remark: str | None = None


class CompanyUpdate(BaseModel):
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    credit_code: str | None = Field(default=None, max_length=64)
    credit_code_type: str | None = Field(default=None, pattern="^(uscc|other)$")
    remark: str | None = None
    participate_analysis: bool | None = None


class FileOrderUpdate(BaseModel):
    file_ids: list[str]
