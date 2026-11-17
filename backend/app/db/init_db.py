import logging
import os
from pathlib import Path

from sqlalchemy import inspect, select, text

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, engine
from app.models import Base, User
from app.models.enums import UserRole

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
BASELINE_REVISION = "0001_baseline"


def ensure_admin(db) -> None:
    user = db.scalars(select(User).where(User.username == settings.admin_username)).first()
    if user:
        return
    admin = User(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        display_name="系统管理员",
        role=UserRole.ADMIN.value,
        is_active=True,
    )
    db.add(admin)
    db.commit()
    logger.info("Created default admin user: %s", settings.admin_username)


def sync_missing_columns() -> None:
    """旧库 reconciliation：给已有表补齐新增的列。

    只在「基线 stamp 之前」对旧库调用一次，用于把 `create_all` 时代的库补齐到
    基线结构。正常启动流程改由 Alembic 管理，不再自动改表——否则模型一改就
    被自动加列，迁移文件却缺失，后续 autogenerate 也看不到差异。
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = (
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                    f"{column.type.compile(dialect=engine.dialect)}"
                )
                if column.server_default is not None:
                    ddl += f" DEFAULT {column.server_default.arg}"
                try:
                    conn.execute(text(ddl))
                    logger.info("Added column %s.%s", table.name, column.name)
                except Exception:  # 例如 NOT NULL 且无默认值的历史数据表
                    logger.exception("Failed to add column %s.%s", table.name, column.name)


def _alembic_config():
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    # ConfigParser 的插值语法要求转义 %
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    return config


def _app_tables_present(inspector) -> bool:
    return inspector.has_table("project") or inspector.has_table("comparison_evidence")


def _upgrade_schema() -> None:
    """按迁移脚本升级数据库结构。

    - 全新库：直接 ``upgrade head``。
    - 旧库（create_all 建表、无 alembic_version）：补齐基线列后
      ``stamp {{基线版本}}``，再从基线升级到 head。
    """
    from alembic import command

    config = _alembic_config()
    inspector = inspect(engine)
    if not inspector.has_table("alembic_version"):
        if _app_tables_present(inspector):
            logger.info("检测到由 create_all 创建的旧库，补齐列并标记基线版本")
            if settings.auto_sync_schema:
                sync_missing_columns()
            missing = _missing_columns(inspector)
            if missing:
                message = "旧库结构与基线不一致：\n" + "\n".join(
                    f"  - {table}.{column}" for table, column in missing
                )
                if os.environ.get("LEGACY_STAMP_OK") != "1":
                    raise RuntimeError(
                        message + "\n确认可以继续（缺失列不影响现有数据）时设置 LEGACY_STAMP_OK=1 重试。"
                    )
                logger.warning(message)
            command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")


def _missing_columns(inspector) -> list[tuple[str, str]]:
    """列出旧库相对基线缺失的列（不自动修改，仅用于提示）。"""
    missing: list[tuple[str, str]] = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            missing.append((table.name, "<整张表>"))
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing:
                missing.append((table.name, column.name))
    return missing


def _schema_mode() -> str:
    mode = (settings.schema_management or "migrate").strip().lower()
    if mode not in {"migrate", "create_all", "auto"}:
        raise RuntimeError(f"未知的 SCHEMA_MANAGEMENT 取值: {mode}")
    if mode == "auto":
        return "create_all" if engine.dialect.name == "sqlite" else "migrate"
    return mode


def ensure_default_rule_versions(db) -> None:
    """为每个检测维度创建出厂规则版本（幂等）。

    规则版本表是空的（例如首次部署或旧库升级）时，用部署配置里的阈值作为
    v1 参数创建并置为生效，这样分析任务始终有规则版本可以快照。
    """
    from app.services import rule_service

    created = rule_service.ensure_initial_version(db)
    if created:
        db.commit()
        logger.info(
            "Created default detection rule versions: %s",
            ", ".join(f"{item.dimension} v{item.version_no}" for item in created),
        )
    upgraded = rule_service.ensure_engine_version(db)
    if upgraded:
        db.commit()
        logger.info(
            "Upgraded text similarity engine rule to v%s", upgraded.version_no
        )
    patched = rule_service.ensure_missing_params(db)
    if patched:
        db.commit()
        logger.info(
            "Added new params to rule versions: %s",
            ", ".join(f"{r.dimension} v{r.version_no}" for r in patched),
        )


def init_db(seed: bool | None = None) -> None:
    if _schema_mode() == "create_all":
        # 测试与 SQLite 本地开发：直接按模型建表，不依赖迁移脚本
        Base.metadata.create_all(bind=engine)
    else:
        _upgrade_schema()
    db = SessionLocal()
    try:
        ensure_admin(db)
        ensure_default_rule_versions(db)
    finally:
        db.close()

    should_seed = settings.seed_demo if seed is None else seed
    if should_seed:
        from app.seed import seed_demo

        db = SessionLocal()
        try:
            seed_demo(db)
            logger.info("Demo project is ready")
        except Exception:
            db.rollback()
            logger.exception("Failed to seed demo data")
            raise
        finally:
            db.close()
