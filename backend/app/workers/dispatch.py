from app.core.config import settings
from app.db.session import SessionLocal


def enqueue_parse(file_id: str) -> None:
    if settings.sync_tasks:
        from app.services import file_service

        db = SessionLocal()
        try:
            file_service.parse_file(db, file_id)
        finally:
            db.close()
        return
    from app.workers.tasks import parse_file_task

    parse_file_task.delay(file_id)


def enqueue_analysis(task_id: str) -> None:
    if settings.sync_tasks:
        from app.services import analysis_service

        db = SessionLocal()
        try:
            analysis_service.run_task(db, task_id)
        finally:
            db.close()
        return
    from app.workers.tasks import run_analysis_task

    run_analysis_task.delay(task_id)


def enqueue_retry_pair(task_id: str, pair_id: str) -> None:
    if settings.sync_tasks:
        from app.models import AnalysisTask, ComparisonPair
        from app.services import analysis_service

        db = SessionLocal()
        try:
            task = db.get(AnalysisTask, task_id)
            pair = db.get(ComparisonPair, pair_id)
            if task and pair:
                analysis_service.retry_pair(db, task, pair)
        finally:
            db.close()
        return
    from app.workers.tasks import retry_pair_task

    retry_pair_task.delay(task_id, pair_id)
