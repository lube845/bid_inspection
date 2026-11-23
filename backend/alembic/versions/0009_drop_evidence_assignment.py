"""drop evidence assignment columns

Revision ID: 0009_drop_evidence_assignment
Revises: 0008_drop_second_review
Create Date: 2026-11-23 10:00:00.000000

取消证据级分派功能：项目内「审核」页已删除，审核改由「比对分析」三栏与
「比对结果」页的项目级证据列表承担，不再支持把证据分派给审核人。因此删除：
- ``comparison_evidence.assignee_id``（含索引 ix_comparison_evidence_assignee_id）
- ``comparison_evidence.assigned_at``

分派属于流程辅助信息，不参与风险判定与导出报告，删除不影响既有审核结论。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_drop_evidence_assignment"
down_revision: Union[str, None] = "0008_drop_second_review"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite 要求先删索引再删列（索引引用该列）。
    with op.batch_alter_table("comparison_evidence", schema=None) as batch_op:
        batch_op.drop_index("ix_comparison_evidence_assignee_id")
        batch_op.drop_column("assigned_at")
        batch_op.drop_column("assignee_id")


def downgrade() -> None:
    # 回退只恢复结构；历史分派关系已丢失，全部为空（未分派）。
    with op.batch_alter_table("comparison_evidence", schema=None) as batch_op:
        batch_op.add_column(sa.Column("assignee_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_comparison_evidence_assignee_id"), ["assignee_id"], unique=False
        )
