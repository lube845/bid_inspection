import logging

from sqlalchemy import inspect, select, text

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, engine
from app.models import Base, User
from app.models.enums import UserRole

logger = logging.getLogger(__name__)


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
    """开发模式：给已有表补齐新增的列。

    只做 ADD COLUMN 这类非破坏性变更，方便开发期改表结构后直接刷新页面看效果；
    涉及删列、改类型、加唯一约束等变更仍需手工处理或 `make reset` 重建数据库。
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


def init_db(seed: bool | None = None) -> None:
    Base.metadata.create_all(bind=engine)
    if settings.auto_sync_schema:
        try:
            sync_missing_columns()
        except Exception:
            logger.exception("Schema sync failed; continue with the current schema")
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
