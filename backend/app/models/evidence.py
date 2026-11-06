from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ReviewStatus, RiskLevel


class ComparisonEvidence(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """比对证据.

    证据同时记录所属项目与分析任务，即使企业组合被重跑，历史证据仍可追溯。
    价格异常这类项目级证据 comparison_pair_id 可为空，参与企业记录在
    evidence_participant 表中。
    """

    __tablename__ = "comparison_evidence"

    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    analysis_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    comparison_pair_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("comparison_pair.id", ondelete="CASCADE"), nullable=True, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    evidence_subtype: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RiskLevel.LOW.value
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_a: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_b: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReviewStatus.PENDING.value, index=True
    )
    # 重跑企业组合时保留旧证据用于追溯，只将 is_current 置为 False
    is_current: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)

    pair = relationship("ComparisonPair", back_populates="evidences")
    participants = relationship(
        "EvidenceParticipant", back_populates="evidence", cascade="all, delete-orphan"
    )
    review_records = relationship(
        "ReviewRecord",
        back_populates="evidence",
        cascade="all, delete-orphan",
        order_by="ReviewRecord.created_at",
    )


class EvidenceParticipant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "evidence_participant"

    evidence_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("comparison_evidence.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="participant")
    source: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    evidence = relationship("ComparisonEvidence", back_populates="participants")
