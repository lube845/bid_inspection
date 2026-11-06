from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "bid_inspection",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

app = celery_app  # 便于 `celery -A app.workers.celery_app worker` 自动发现

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=86400,
)
