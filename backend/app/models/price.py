"""人工确认的投标报价.

这里只存**人工权威输入**（导入或补录的报价），不存自动抽取结果。
自动抽取属于任务状态，会固化进 ``analysis_task.analysis_config["price_snapshot"]``，
否则每次重跑都会写项目级行，重试与历史对比的语义就乱了。

改价不覆盖旧行：旧行置为无效并记录 ``superseded_at``，同时插入
``version_no + 1`` 的新行。这与 ``review_record`` / ``original_file`` 的追加式
风格一致，历史任务仍能引用当时的版本。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PriceQuote(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "price_quote"
    __table_args__ = (
        # 同一企业每个版本只有一条；「当前生效」由 is_active 标识，
        # 唯一性在服务层保证（SQLite 对部分唯一索引的支持不如 Postgres 稳定）。
        UniqueConstraint("project_id", "company_id", "version_no", name="uq_price_quote_version"),
    )

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 金额用 Numeric 而不是 Float：报价需要精确到分，浮点误差会直接影响
    # 偏差率、等差判定与大小写一致性校验。
    amount: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    # 投标文件里书写的大写金额，用于大小写一致性校验；可为空
    amount_upper: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    # 导入原文行 / 手工备注
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # manual | import
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    # 批次号、表格行号，以及可选的文件/页码定位
    source_ref: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
