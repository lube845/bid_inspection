"""user identity fields and two-role model

Revision ID: 0006_user_identity
Revises: 0005_price_item
Create Date: 2026-11-18 15:00:00.000000

1. 用户新增工号（employee_no）与邮箱（email）。**登录只认这两项**，因此它们各自唯一；
   用户名改成可重复（同名同事是常态），从唯一索引降为普通索引。
2. 角色从三种（admin / analyst / reviewer）收敛为两种（admin / user）：
   审核权限不再由角色区分，现有 analyst / reviewer 一律改为 user。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_user_identity"
down_revision: Union[str, None] = "0005_price_item"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("employee_no", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("email", sa.String(length=254), nullable=True))
        # 唯一但允许多个 NULL：没填工号/邮箱的账号不互相冲突
        batch_op.create_index("ix_app_user_employee_no", ["employee_no"], unique=True)
        batch_op.create_index("ix_app_user_email", ["email"], unique=True)
        # 用户名不再承担登录，允许重名：唯一索引换成普通索引
        batch_op.drop_index("ix_app_user_username")
        batch_op.create_index("ix_app_user_username", ["username"], unique=False)

    # 旧角色收敛：审核与分析的区分不再存在
    op.execute("UPDATE app_user SET role = 'user' WHERE role IN ('analyst', 'reviewer')")

    # 存量账号没有工号/邮箱，用用户名补一个工号，满足「工号与邮箱至少一个」
    op.execute(
        "UPDATE app_user SET employee_no = username "
        "WHERE employee_no IS NULL AND email IS NULL"
    )


def downgrade() -> None:
    # 角色回退会把 user 展开成 analyst（原先的 reviewer 无法还原，统一按 analyst 处理）
    op.execute("UPDATE app_user SET role = 'analyst' WHERE role = 'user'")

    with op.batch_alter_table("app_user", schema=None) as batch_op:
        # 回退成用户名唯一：重名数据会阻止降级，需先人工合并/改名
        batch_op.drop_index("ix_app_user_username")
        batch_op.create_index("ix_app_user_username", ["username"], unique=True)
        batch_op.drop_index("ix_app_user_email")
        batch_op.drop_index("ix_app_user_employee_no")
        batch_op.drop_column("email")
        batch_op.drop_column("employee_no")
