"""经验模板库：人工确认的「行业范本 / 通用套话」片段.

批次共同模板（``analysis/template.py``）只能识别「本次这一批里多家都有」的内容；
本表把审核人的判断沉淀下来，让同一个片段在以后的任务里直接降权，误报率因此
随使用下降。

匹配以 Shingle 集合为主（快），同时保留规范化原文（可展示、可审计、可在
``shingle_size`` 变更后重建）。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TemplateFragment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "template_fragment"

    # project：只对本项目生效；global：全系统生效（需管理员）
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="project", index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shingle_size: Mapped[int] = mapped_column(Integer, nullable=False)
    shingle_hashes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    informativeness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
