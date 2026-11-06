from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ProjectStatus


class Project(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "project"

    project_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    project_code: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProjectStatus.PENDING.value, index=True
    )
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("app_user.id"), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    companies = relationship(
        "Company", back_populates="project", cascade="all, delete-orphan", order_by="Company.sort_order"
    )
    files = relationship("OriginalFile", back_populates="project", cascade="all, delete-orphan")
    tasks = relationship("AnalysisTask", back_populates="project", cascade="all, delete-orphan")
