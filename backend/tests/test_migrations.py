"""迁移脚本与模型定义的一致性检查.

这条测试拦住最常见的一类问题：改了模型却忘了生成迁移脚本。做法是在临时
SQLite 库上跑 ``alembic upgrade head``，再把迁移后的结构与 ``Base.metadata``
做 autogenerate 比对，期望没有任何差异。

SQLite 的类型回读与 Postgres 不同（如 Boolean 回读为 INTEGER），因此
``compare_type=False``，只比对表/列/索引/约束层面的存在性。
"""

from __future__ import annotations

import os
import tempfile

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.db.init_db import ALEMBIC_INI, BACKEND_ROOT
from app.models import Base

_IGNORED_TABLES = {"alembic_version"}


def _config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _normalize(diffs: list) -> list[str]:
    """把 compare_metadata 的结果压成可读字符串，并过滤无关差异。"""
    normalised: list[str] = []
    for diff in diffs:
        text = repr(diff)
        if any(table in text for table in _IGNORED_TABLES):
            continue
        normalised.append(text)
    return normalised


def test_migrations_match_models() -> None:
    with tempfile.TemporaryDirectory(prefix="bid-migrations-") as tmp:
        db_path = os.path.join(tmp, "migrate.db")
        url = f"sqlite:///{db_path}"
        command.upgrade(_config(url), "head")

        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                context = MigrationContext.configure(
                    connection,
                    opts={"compare_type": False, "compare_server_default": False},
                )
                diffs = compare_metadata(context, Base.metadata)
        finally:
            engine.dispose()

    assert _normalize(diffs) == [], (
        "模型与迁移脚本不一致，请执行 `make revision m=\"...\"` 生成迁移：\n"
        + "\n".join(_normalize(diffs))
    )
