"""revocable tokens

Revision ID: 0003_token_revocation
Revises: 0002_features
Create Date: 2026-11-17 15:00:00.000000

退出登录 / 重置密码 / 禁用账号时递增 app_user.token_version，签发的 JWT 与
短期票据带当时版本号，校验时对不上即失效。无状态 JWT 无法逐条撤回，这是
最小代价的撤销机制。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_token_revocation"
down_revision: Union[str, None] = "0002_features"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("app_user", schema=None) as batch_op:
        batch_op.drop_column("token_version")
