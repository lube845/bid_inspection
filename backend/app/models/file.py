from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.models.enums import FileKind, FileParseStatus


class OriginalFile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """原始文件表.

    招标文件的 company_id 为空；企业投标文件的 company_id 必有值。
    替换文件时新建一条记录并令旧记录 is_active=False，绝不覆盖历史文件。
    """

    __tablename__ = "original_file"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("company.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=FileKind.BID.value)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parse_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=FileParseStatus.PENDING.value, index=True
    )
    parse_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    superseded_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    project = relationship("Project", back_populates="files")
    company = relationship("Company", back_populates="files")
    pages = relationship(
        "DocumentPage",
        back_populates="original_file",
        cascade="all, delete-orphan",
        order_by="DocumentPage.page_index",
    )
