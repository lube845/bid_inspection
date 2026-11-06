from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import LogicalDocStatus


class LogicalDocument(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """企业的逻辑标书：只组织文件顺序，不复制、不修改原始文件。"""

    __tablename__ = "logical_document"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("company.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=LogicalDocStatus.NOT_GENERATED.value
    )

    company = relationship("Company", back_populates="logical_document")
    versions = relationship(
        "LogicalDocumentVersion",
        back_populates="logical_document",
        cascade="all, delete-orphan",
        order_by="LogicalDocumentVersion.version_no",
    )


class LogicalDocumentVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "logical_document_version"

    logical_document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("logical_document.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="generated")

    logical_document = relationship("LogicalDocument", back_populates="versions")
    items = relationship(
        "LogicalDocumentItem",
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="LogicalDocumentItem.sort_order",
    )


class LogicalDocumentItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "logical_document_item"

    logical_document_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("logical_document_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    original_file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("original_file.id"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    version = relationship("LogicalDocumentVersion", back_populates="items")
