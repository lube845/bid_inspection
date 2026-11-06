from pydantic import BaseModel, Field, field_validator

from app.models.enums import Dimension


class StartAnalysisRequest(BaseModel):
    company_ids: list[str] | None = None
    # 可选：只使用企业部分文件参与分析；不传则使用全部已解析有效文件
    company_file_ids: dict[str, list[str]] | None = None
    dimensions: list[str] = Field(
        default_factory=lambda: [Dimension.TEXT.value, Dimension.PERSONNEL.value, Dimension.PRICE.value]
    )
    exclude_tender_public_content: bool = True
    confirm_partial: bool = False

    @field_validator("dimensions")
    @classmethod
    def validate_dimensions(cls, value: list[str]) -> list[str]:
        allowed = {d.value for d in Dimension}
        invalid = set(value) - allowed
        if invalid:
            raise ValueError(f"不支持的分析维度: {', '.join(sorted(invalid))}")
        if not value:
            raise ValueError("至少选择一个分析维度")
        return list(dict.fromkeys(value))


class ReviewRequest(BaseModel):
    status: str = Field(pattern="^(confirmed|excluded|undecided)$")
    comment: str | None = None
