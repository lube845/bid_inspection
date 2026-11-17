"""company credit code type

Revision ID: 0004_credit_code_type
Revises: 0003_token_revocation
Create Date: 2026-11-17 15:30:00.000000

企业证件类型：uscc = 统一社会信用代码（校验 18 位格式与校验位），
other = 其他证件（外国企业等），显式选择后跳过统一社会信用代码校验。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_credit_code_type"
down_revision: Union[str, None] = "0003_token_revocation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("company", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "credit_code_type", sa.String(length=16), nullable=False, server_default="uscc"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("company", schema=None) as batch_op:
        batch_op.drop_column("credit_code_type")
