import logging

from app.db.session import SessionLocal
from app.services import analysis_service, file_service
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks.parse_file_task", bind=True, max_retries=2)
def parse_file_task(self, file_id: str) -> None:
    db = SessionLocal()
    try:
        file = file_service.parse_file(db, file_id)
        logger.info("Parsed file %s -> %s", file_id, file.parse_status)
    except Exception as exc:
        logger.exception("Failed to parse file %s", file_id)
        db.rollback()
        raise self.retry(exc=exc, countdown=10)
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.run_analysis_task", bind=True, max_retries=1)
def run_analysis_task(self, task_id: str) -> None:
    db = SessionLocal()
    try:
        task = analysis_service.run_task(db, task_id)
        logger.info("Analysis task %s finished with %s", task_id, task.status)
    except Exception as exc:
        logger.exception("Analysis task %s failed", task_id)
        db.rollback()
        raise self.retry(exc=exc, countdown=15)
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.retry_pair_task")
def retry_pair_task(task_id: str, pair_id: str) -> None:
    from app.models import AnalysisTask, ComparisonPair

    db = SessionLocal()
    try:
        task = db.get(AnalysisTask, task_id)
        pair = db.get(ComparisonPair, pair_id)
        if task and pair:
            analysis_service.retry_pair(db, task, pair)
    except Exception:
        logger.exception("Retry pair %s of task %s failed", pair_id, task_id)
        db.rollback()
    finally:
        db.close()
