"""drop second review (two-person review workflow)

Revision ID: 0008_drop_second_review
Revises: 0007_project_member
Create Date: 2026-11-19 10:00:00.000000

取消「两位审核人复核」：审核改为单级，一次审核即结论。因此删除：
- ``project.requires_second_review``  项目级开关
- ``comparison_evidence.review_stage`` 流程位置（0 未审 / 1 待二审 / 2 终审）
- ``comparison_evidence.first_reviewer_id / first_review_status`` 一审快照
- ``review_record.stage``              审核记录的流程位置

升级前库里 ``review_stage`` 只有 0 与 2（没有等待二审的证据），
``review_status`` 因此可以原样当作唯一结论使用，不需要数据迁移。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_drop_second_review"
down_revision: Union[str, None] = "0007_project_member"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("comparison_evidence", schema=None) as batch_op:
        batch_op.drop_index("ix_comparison_evidence_review_stage")
        batch_op.drop_column("first_review_status")
        batch_op.drop_column("first_reviewer_id")
        batch_op.drop_column("review_stage")

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("requires_second_review")

    with op.batch_alter_table("review_record", schema=None) as batch_op:
        batch_op.drop_column("stage")


def downgrade() -> None:
    # 回退只恢复结构；「一审/终审」的历史信息已丢失，全部按单级审核重建。
    # 证据的 review_stage 用 review_status 推导：pending → 0，其余 → 2。
    with op.batch_alter_table("review_record", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stage", sa.Integer(), nullable=False, server_default="1"))
        batch_op.alter_column("stage", server_default=None)

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "requires_second_review", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.alter_column("requires_second_review", server_default=None)

    with op.batch_alter_table("comparison_evidence", schema=None) as batch_op:
        batch_op.add_column(sa.Column("review_stage", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("first_reviewer_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("first_review_status", sa.String(length=16), nullable=True))
        batch_op.create_index("ix_comparison_evidence_review_stage", ["review_stage"])
        batch_op.alter_column("review_stage", server_default=None)

    op.execute(
        "UPDATE comparison_evidence SET review_stage = 2 "
        "WHERE review_status <> 'pending'"
    )
