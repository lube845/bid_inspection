"""price item (per-item unit prices)

Revision ID: 0005_price_item
Revises: 0004_credit_code_type
Create Date: 2026-11-18 10:00:00.000000

新增「清单分项单价」表：企业上传的价格 Excel 除了总报价，还能带逐项单价。
价格比对矩阵按清单项跨企业对齐，需要这部分数据落在库里；正文自动识别的
分项单价也复用同一张表（source_kind=auto），保证矩阵与证据同源。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_price_item"
down_revision: Union[str, None] = "0004_credit_code_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("item_key", sa.String(length=255), nullable=False),
        sa.Column("item_name", sa.String(length=255), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("source_kind", sa.String(length=16), nullable=False, server_default="import"),
        sa.Column("source_ref", sa.JSON(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "company_id", "item_key", "version_no",
            name="uq_price_item_version",
        ),
    )
    op.create_index("ix_price_item_project_id", "price_item", ["project_id"])
    op.create_index("ix_price_item_company_id", "price_item", ["company_id"])
    op.create_index("ix_price_item_item_key", "price_item", ["item_key"])
    op.create_index("ix_price_item_is_active", "price_item", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_price_item_is_active", table_name="price_item")
    op.drop_index("ix_price_item_item_key", table_name="price_item")
    op.drop_index("ix_price_item_company_id", table_name="price_item")
    op.drop_index("ix_price_item_project_id", table_name="price_item")
    op.drop_table("price_item")
