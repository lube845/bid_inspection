from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.models.enums import PairStatus, RiskLevel, TaskStatus


class AnalysisTask(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "analysis_task"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaskStatus.QUEUED.value, index=True
    )
    analysis_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tender_file_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    company_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    project = relationship("Project", back_populates="tasks")
    pairs = relationship(
        "ComparisonPair", back_populates="task", cascade="all, delete-orphan"
    )
    company_snapshots = relationship(
        "AnalysisCompanySnapshot", back_populates="task", cascade="all, delete-orphan"
    )
    logs = relationship(
        "AnalysisTaskLog", back_populates="task", cascade="all, delete-orphan",
        order_by="AnalysisTaskLog.created_at",
    )


class AnalysisTaskLog(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "analysis_task_log"

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    task = relationship("AnalysisTask", back_populates="logs")


class AnalysisCompanySnapshot(Base, UUIDPrimaryKeyMixin):
    """分析快照：记录本次分析中每家企业使用的文件版本与逻辑标书版本。"""

    __tablename__ = "analysis_company_snapshot"

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    logical_document_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    file_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    files: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    complete: Mapped[bool] = mapped_column(default=True, nullable=False)

    task = relationship("AnalysisTask", back_populates="company_snapshots")


class ComparisonPair(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "comparison_pair"
    __table_args__ = (
        UniqueConstraint(
            "analysis_task_id", "company_a_id", "company_b_id", name="uq_pair_task_companies"
        ),
    )

    analysis_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_a_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    company_b_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    text_risk: Mapped[str] = mapped_column(String(16), nullable=False, default=RiskLevel.NONE.value)
    personnel_risk: Mapped[str] = mapped_column(String(16), nullable=False, default=RiskLevel.NONE.value)
    price_risk: Mapped[str] = mapped_column(String(16), nullable=False, default=RiskLevel.NONE.value)
    overall_risk: Mapped[str] = mapped_column(String(16), nullable=False, default=RiskLevel.NONE.value)
    risk_reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PairStatus.PENDING.value
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    task = relationship("AnalysisTask", back_populates="pairs")
    evidences = relationship(
        "ComparisonEvidence", back_populates="pair", cascade="all, delete-orphan"
    )
    results = relationship(
        "AnalysisPairResult", back_populates="pair", cascade="all, delete-orphan"
    )


class TextSimilarityResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """文本块对的 Jaccard 精确计算结果（方案 12.1 text_similarity_result）.

    每个企业组合下，通过候选召回并完成局部对齐的文本块对保存一条记录。
    原始 Shingle 集合不落库，可通过 chunk 指纹和算法版本重新计算。
    """

    __tablename__ = "text_similarity_result"

    analysis_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pair_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("comparison_pair.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_a_id: Mapped[str] = mapped_column(String(36), nullable=False)
    company_b_id: Mapped[str] = mapped_column(String(36), nullable=False)
    chunk_a_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chunk_b_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    file_a_id: Mapped[str] = mapped_column(String(36), nullable=False)
    file_b_id: Mapped[str] = mapped_column(String(36), nullable=False)
    jaccard_similarity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    effective_similarity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    containment: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    intersection_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    union_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effective_intersection_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effective_union_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    baseline_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    matched_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_local_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    excluded_by_tender: Mapped[bool] = mapped_column(default=False, nullable=False)
    localization_status: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    result_status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")


class AnalysisPairResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "analysis_pair_result"

    pair_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("comparison_pair.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dimension: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pair = relationship("ComparisonPair", back_populates="results")
