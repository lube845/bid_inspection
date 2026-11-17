from pydantic import BaseModel, Field


class QuoteEntry(BaseModel):
    amount: float = Field(gt=0)
    amount_upper: str | None = Field(default=None, max_length=128)
    remark: str | None = Field(default=None, max_length=2000)
