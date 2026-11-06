import logging
import time

from app.db.init_db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("bootstrap")


def main() -> None:
    last_error: Exception | None = None
    for attempt in range(1, 31):
        try:
            init_db()
            logger.info("Database initialized")
            return
        except Exception as exc:  # 等待数据库就绪
            last_error = exc
            logger.warning("Database not ready (attempt %s/30): %s", attempt, exc)
            time.sleep(2)
    raise SystemExit(f"Database initialization failed: {last_error}")


if __name__ == "__main__":
    main()
