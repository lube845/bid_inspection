"""项目成员与访问权限.

项目默认只有创建者可见；创建者可以把项目分享给其他用户，并授予三种权限之一：

- ``viewer``  查看：看项目所有内容，但不能审核、不能改动；
- ``reviewer`` 审批：在查看基础上可以审核/分派证据；
- ``manager`` 管理：可以改项目、管企业/文件/报价、发起分析、继续分享给他人。

系统管理员对全部项目拥有管理权限；创建者本人始终是管理者。
"""

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProjectMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "project_member"
    __table_args__ = (
        # 同一个用户在一个项目里只能有一条授权记录
        UniqueConstraint("project_id", "user_id", name="uq_project_member_project_user"),
    )

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer")
    granted_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
