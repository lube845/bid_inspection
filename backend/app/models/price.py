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


class PriceItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """清单分项单价（企业级，来自价格 Excel 或正文自动识别）。

    与 :class:`PriceQuote` 的关系是「一个总价 + 多个分项」：总价仍走报价表，
    分项单独成表，这样价格比对矩阵可以跨企业按清单项对齐。

    ``source_kind`` 区分权威程度：``import``（价格 Excel）> ``manual``（补录）
    > ``auto``（正文识别）。矩阵与证据都按这个顺序取当前有效版本。
    """

    __tablename__ = "price_item"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "company_id", "item_key", "version_no",
            name="uq_price_item_version",
        ),
    )

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 规范化后的清单项名称，跨企业对齐用
    item_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="import")
    # 批次号、原始行号、文件与页码定位
    source_ref: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
