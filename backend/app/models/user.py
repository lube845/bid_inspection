from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "app_user"

    # 身份字段：用户名可重复（同名同事是常态），**登录只用工号或邮箱**，
    # 因此只有工号与邮箱建唯一索引。工号与邮箱至少填一个（业务层校验）。
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    employee_no: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=UserRole.USER.value)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    # 令牌版本号：每次退出登录 / 重置密码 / 禁用账号时 +1，签发的 JWT 里带着
    # 当时的版本；校验时对不上即视为已吊销。无状态 JWT 本身无法撤回，这是
    # 在不引入服务端会话表的前提下最小代价的撤销机制。
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
