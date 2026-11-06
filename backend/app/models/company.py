from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import CompanyStatus


class Company(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "company"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    credit_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CompanyStatus.INCOMPLETE.value
    )
    participate_analysis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project = relationship("Project", back_populates="companies")
    files = relationship(
        "OriginalFile", back_populates="company", cascade="all, delete-orphan"
    )
    logical_document = relationship(
        "LogicalDocument",
        back_populates="company",
        cascade="all, delete-orphan",
        uselist=False,
    )
