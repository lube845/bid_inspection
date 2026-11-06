import os
import tempfile

import pytest

_TEST_ROOT = tempfile.mkdtemp(prefix="bid-inspection-tests-")
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{_TEST_ROOT}/test.db",
        "STORAGE_ROOT": f"{_TEST_ROOT}/storage",
        "SYNC_TASKS": "true",
        "SEED_DEMO": "false",
        "SECRET_KEY": "test-secret",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "admin123",
    }
)

from app.db.session import SessionLocal, engine  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
