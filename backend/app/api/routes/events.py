"""项目实时事件流（SSE）.

用于开发模式和前端实时展示：解析进度、分析任务进度、证据生成情况每秒检查一次，
只在快照发生变化时推送，避免无效刷新。生产环境同样可用（nginx 已关闭代理缓冲）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import get_stream_user
from app.db.session import SessionLocal
from app.models import (
    AnalysisTask,
    ComparisonEvidence,
    ComparisonPair,
    OriginalFile,
    Project,
    User,
)
from app.models.enums import FileParseStatus, PairStatus, ReviewStatus


router = APIRouter(tags=["events"])


def _build_snapshot(project_id: str) -> dict:
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if not project or project.deleted_at:
            return {"type": "error", "message": "项目不存在"}

        files = list(
            db.scalars(
                select(OriginalFile).where(
                    OriginalFile.project_id == project_id,
                    OriginalFile.is_active.is_(True),
                )
            )
        )
        parse_counts: Counter = Counter(file.parse_status for file in files)
        parse_payload = {status.value: parse_counts.get(status.value, 0) for status in FileParseStatus}
        parse_payload["total"] = len(files)
        parsing = any(
            file.parse_status
            in {
                FileParseStatus.UPLOADING.value,
                FileParseStatus.PENDING.value,
                FileParseStatus.PARSING.value,
            }
            for file in files
        )

        task = db.scalars(
            select(AnalysisTask)
            .where(AnalysisTask.project_id == project_id)
            .order_by(AnalysisTask.created_at.desc())
            .limit(1)
        ).first()

        task_payload = None
        if task:
            pair_total = db.scalar(
                select(func.count())
                .select_from(ComparisonPair)
                .where(ComparisonPair.analysis_task_id == task.id)
            ) or 0
            pair_completed = db.scalar(
                select(func.count())
                .select_from(ComparisonPair)
                .where(
                    ComparisonPair.analysis_task_id == task.id,
                    ComparisonPair.status == PairStatus.COMPLETED.value,
                )
            ) or 0
            pair_failed = db.scalar(
                select(func.count())
                .select_from(ComparisonPair)
                .where(
                    ComparisonPair.analysis_task_id == task.id,
                    ComparisonPair.status == PairStatus.FAILED.value,
                )
            ) or 0
            evidence_count = db.scalar(
                select(func.count())
                .select_from(ComparisonEvidence)
                .where(
                    ComparisonEvidence.analysis_task_id == task.id,
                    ComparisonEvidence.is_current.is_(True),
                )
            ) or 0
            pending_review = db.scalar(
                select(func.count())
                .select_from(ComparisonEvidence)
                .where(
                    ComparisonEvidence.analysis_task_id == task.id,
                    ComparisonEvidence.is_current.is_(True),
                    ComparisonEvidence.review_status == ReviewStatus.PENDING.value,
                )
            ) or 0
            task_payload = {
                "id": task.id,
                "task_no": task.task_no,
                "status": task.status,
                "progress": task.progress or {},
                "pair_count": pair_total,
                "completed_pair_count": pair_completed,
                "failed_pair_count": pair_failed,
                "evidence_count": evidence_count,
                "pending_review_count": pending_review,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "updated_at": task.updated_at,
            }

        return {
            "type": "snapshot",
            "project_id": project.id,
            "project_status": project.status,
            "parse": parse_payload,
            "parsing": parsing,
            "task": task_payload,
            "server_time": time.time(),
        }
    finally:
        db.close()


@router.get("/projects/{project_id}/events")
async def project_events(
    project_id: str,
    user: User = Depends(get_stream_user),
) -> StreamingResponse:
    async def generator():
        last_payload: str | None = None
        last_ping = time.monotonic()
        yield "retry: 3000\n\n"
        while True:
            snapshot = await asyncio.to_thread(_build_snapshot, project_id)
            if snapshot.get("type") == "error":
                yield f"event: error\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                return
            # server_time 每秒都在变，比较时剔除它，否则每次都会推送
            comparable = {key: value for key, value in snapshot.items() if key != "server_time"}
            payload = json.dumps(comparable, ensure_ascii=False, default=str, sort_keys=True)
            if payload != last_payload:
                last_payload = payload
                yield f"event: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False, default=str, sort_keys=True)}\n\n"
            elif time.monotonic() - last_ping >= 15:
                last_ping = time.monotonic()
                yield ": ping\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
