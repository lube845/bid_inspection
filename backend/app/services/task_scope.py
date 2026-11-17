"""证据的「当前结论」口径.

同一个项目可以反复分析：每次分析都会新建一个 ``analysis_task``，并把新证据
的 ``is_current`` 置为 True。``is_current`` 只表示「这条证据在该任务内没有被
重跑覆盖」，并不能区分任务——如果统计/列表不加任务范围，4 次分析的最新证据
会与历史任务的证据一起进入审核队列（实测 18 条被累计成 87 条）。

因此凡是「当前待办 / 当前风险」口径的查询，都必须同时限定到每个项目的最新
分析任务。历史任务仍可通过显式参数查看，但不能混入默认列表。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AnalysisTask


def latest_task_ids(
    db: Session, project_ids: list[str] | None = None
) -> dict[str, str]:
    """每个项目最新的分析任务 id（按创建时间，与状态无关）。

    返回 ``{project_id: task_id}``。没有任务的项目不出现在结果里。
    """
    if project_ids is not None and not project_ids:
        return {}
    query = select(
        AnalysisTask.project_id,
        func.max(AnalysisTask.created_at),
    )
    if project_ids:
        query = query.where(AnalysisTask.project_id.in_(project_ids))
    latest_created = dict(db.execute(query.group_by(AnalysisTask.project_id)).all())
    if not latest_created:
        return {}

    task_query = select(
        AnalysisTask.project_id, AnalysisTask.id, AnalysisTask.created_at
    ).where(AnalysisTask.project_id.in_(list(latest_created)))
    if project_ids:
        task_query = task_query.where(AnalysisTask.project_id.in_(project_ids))

    result: dict[str, str] = {}
    for project_id, task_id, created_at in db.execute(task_query).all():
        if created_at == latest_created.get(project_id):
            result.setdefault(project_id, task_id)
    return result


def latest_task_id(db: Session, project_id: str) -> str | None:
    return latest_task_ids(db, [project_id]).get(project_id)
