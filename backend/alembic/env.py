"""Alembic 运行环境.

数据库地址统一从 ``app.core.config.settings.database_url`` 读取：
容器里是 Postgres，测试里是 SQLite，二者用同一套迁移脚本。
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

target_metadata = Base.metadata


def _database_url() -> str:
    """优先使用调用方显式设置的 URL（测试 / 多库运维），否则取应用配置。

    ConfigParser 会把 % 当插值语法，URL 里的 % 需要转义。
    """
    configured = config.get_main_option("sqlalchemy.url", None)
    url = configured or settings.database_url
    return url.replace("%", "%%")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite 不支持大部分 ALTER，需要批量重建表
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
