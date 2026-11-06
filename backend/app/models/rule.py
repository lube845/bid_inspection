from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import RuleStatus


class DetectionRuleVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """检测规则版本。

    按维度（文本相似 / 人员重复 / 价格异常）保存一份参数快照。每次修改规则生成一条
    新版本记录，旧版本置为 archived 保留（不删除），因此历史分析任务可以通过
    analysis_config["rule_versions"] 里的快照继续使用启动时的规则。
    """

    __tablename__ = "detection_rule_version"

    dimension: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 该维度内的版本号，从 1 开始单调递增
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 全局批次号：一次提交涉及多个维度时共用同一个批次号
    batch_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RuleStatus.DRAFT.value, index=True
    )
    # 修改后内容
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 修改前内容（新建时取当前生效版本的内容）
    previous_params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本版本取代的版本 id
    supersedes_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
