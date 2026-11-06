from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ExtractionMethod, PageStatus


class DocumentPage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "document_page"

    original_file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("original_file.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_page_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extraction_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ExtractionMethod.NATIVE.value
    )
    parse_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PageStatus.SUCCESS.value
    )
    page_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    page_height: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)

    original_file = relationship("OriginalFile", back_populates="pages")
    blocks = relationship(
        "DocumentBlock",
        back_populates="page",
        cascade="all, delete-orphan",
        order_by="DocumentBlock.block_index",
    )


class DocumentBlock(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "document_block"

    document_page_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_page.id", ondelete="CASCADE"), nullable=False, index=True
    )
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_type: Mapped[str] = mapped_column(String(32), nullable=False, default="paragraph")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    section_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_body_text: Mapped[bool] = mapped_column(default=True, nullable=False)

    page = relationship("DocumentPage", back_populates="blocks")
