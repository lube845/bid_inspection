from pydantic import BaseModel, Field


class BatchReviewRequest(BaseModel):
    """批量审核。

    V1.0 只允许待进一步核查（undecided）。这里不在 schema 层限制取值，
    交由服务层给出明确的中文提示（PRD 4.11.5）。
    """

    evidence_ids: list[str] = Field(min_length=1)
    status: str = Field(default="undecided")
    comment: str | None = Field(default=None, max_length=1000)
