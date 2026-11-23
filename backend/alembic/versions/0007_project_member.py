"""project member (share project with users)

Revision ID: 0007_project_member
Revises: 0006_user_identity
Create Date: 2026-11-18 18:00:00.000000

项目默认只对创建者可见，新增 project_member 表记录「分享给谁 + 什么权限」。
管理员不需要逐条授权，创建者也不写入该表（二者在权限服务里直接判定），
因此表里只放真正被分享出去的用户。

历史项目（0007 之前的）没有归属记录：创建者字段为空的项目设计为
「仅系统管理员可见」，避免升级后所有历史项目突然对全部用户开放。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_project_member"
down_revision: Union[str, None] = "0006_user_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_member",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="viewer"),
        sa.Column("granted_by", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["app_user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "user_id", name="uq_project_member_project_user"
        ),
    )
    op.create_index("ix_project_member_project_id", "project_member", ["project_id"])
    op.create_index("ix_project_member_user_id", "project_member", ["user_id"])

    # 历史项目没有创建者：把有创建者的项目的隐式归属保留下来（创建者本身就是管理者，
    # 不需要写表）；无创建者的项目保持「仅管理员可见」，符合更严格的默认。
    # 这里不写任何数据行——分享关系只能由创建者/管理员主动建立。


def downgrade() -> None:
    op.drop_index("ix_project_member_user_id", table_name="project_member")
    op.drop_index("ix_project_member_project_id", table_name="project_member")
    op.drop_table("project_member")
