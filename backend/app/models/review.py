from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class ReviewRecord(Base, UUIDPrimaryKeyMixin):
    """人工审核记录。只允许追加，不允许修改历史。"""

    __tablename__ = "review_record"

    evidence_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("comparison_evidence.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewer_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    old_status: Mapped[str] = mapped_column(String(16), nullable=False)
    new_status: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    evidence = relationship("ComparisonEvidence", back_populates="review_records")


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_log"

    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
