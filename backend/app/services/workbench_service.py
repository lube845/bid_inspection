"""全局工作台（PRD 4.1）。

汇总当前用户可见的全部项目、分析任务与异常证据，给出全局统计、待办清单、
最近项目/任务以及风险分布。

设计要点：
1. 全部聚合都用 group-by 查询，SQL 数量固定，与项目数量无关（不逐项目调
   project_stats，避免 N+1）。
2. 证据统计一律带 is_current 过滤，且只数 comparison_evidence 的行；
   绝不 JOIN evidence_participant —— 一条涉及 3 家企业的项目级价格证据
   被 JOIN 就会数成 3 条（PRD 4.1.2 要求同一证据不得重复计数）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, false, func, select
from sqlalchemy.orm import Session

from app.models import (
    AnalysisTask,
    AuditLog,
    Company,
    ComparisonEvidence,
    OriginalFile,
    Project,
)
from app.models.enums import (
    EvidenceType,
    FileKind,
    FileParseStatus,
    ProjectStatus,
    ReviewStatus,
    RiskLevel,
    TaskStatus,
)
from app.services import project_service

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "none": 3}
SEVERITY_LABELS = {
    RiskLevel.HIGH.value: "高",
    RiskLevel.MEDIUM.value: "中",
    RiskLevel.LOW.value: "低",
    RiskLevel.NONE.value: "无",
}


def summary(
    db: Session,
    project_limit: int = 8,
    task_limit: int = 8,
    todo_limit: int = 30,
) -> dict:
    projects = list(
        db.scalars(select(Project).where(Project.deleted_at.is_(None)))
    )
    project_ids = [project.id for project in projects]
    # 所有统计都限定在未删除的项目内，保证「项目总数」与证据/任务口径一致
    in_projects = (
        AnalysisTask.project_id.in_(project_ids) if project_ids else false()
    )

    # ---- 任务统计 -------------------------------------------------------
    task_counts = dict(
        db.execute(
            select(AnalysisTask.status, func.count())
            .where(in_projects)
            .group_by(AnalysisTask.status)
        ).all()
    )
    task_completed = task_counts.get(TaskStatus.COMPLETED.value, 0)
    task_partial = task_counts.get(TaskStatus.PARTIAL.value, 0)

    # ---- 证据统计（注意：全部带 is_current，且不 JOIN participant）--------
    # 项目范围为空时（全部项目已删除）统计结果应为零
    current_filter = and_(
        ComparisonEvidence.is_current.is_(True),
        ComparisonEvidence.project_id.in_(project_ids) if project_ids else false(),
    )
    evidence_total = (
        db.scalar(
            select(func.count()).select_from(ComparisonEvidence).where(current_filter)
        )
        or 0
    )
    evidence_pending = (
        db.scalar(
            select(func.count())
            .select_from(ComparisonEvidence)
            .where(current_filter, ComparisonEvidence.review_status == ReviewStatus.PENDING.value)
        )
        or 0
    )
    by_type = _count_by(db, ComparisonEvidence.evidence_type, current_filter)
    by_severity = _count_by(db, ComparisonEvidence.severity, current_filter)
    by_review = _count_by(db, ComparisonEvidence.review_status, current_filter)
    by_project = _count_by(db, ComparisonEvidence.project_id, current_filter)
    pending_by_project = dict(
        db.execute(
            select(ComparisonEvidence.project_id, func.count())
            .where(current_filter, ComparisonEvidence.review_status == ReviewStatus.PENDING.value)
            .group_by(ComparisonEvidence.project_id)
        ).all()
    )

    # ---- 文件解析状态 ----------------------------------------------------
    file_rows = db.execute(
        select(OriginalFile.project_id, OriginalFile.parse_status, func.count())
        .where(
            OriginalFile.is_active.is_(True),
            OriginalFile.kind == FileKind.BID.value,
            OriginalFile.project_id.in_(project_ids),
        )
        .group_by(OriginalFile.project_id, OriginalFile.parse_status)
    ).all() if project_ids else []
    file_status_by_project: dict[str, dict[str, int]] = {}
    for project_id, parse_status, count in file_rows:
        file_status_by_project.setdefault(project_id, {})[parse_status] = count

    problem_files = list(
        db.scalars(
            select(OriginalFile).where(
                OriginalFile.is_active.is_(True),
                OriginalFile.kind == FileKind.BID.value,
                OriginalFile.project_id.in_(project_ids),
                OriginalFile.parse_status.in_(
                    [FileParseStatus.FAILED.value, FileParseStatus.PARTIAL.value]
                ),
            )
        )
    ) if project_ids else []
    failed_by_project: dict[str, list[OriginalFile]] = {}
    partial_by_project: dict[str, list[OriginalFile]] = {}
    for file in problem_files:
        bucket = (
            failed_by_project
            if file.parse_status == FileParseStatus.FAILED.value
            else partial_by_project
        )
        bucket.setdefault(file.project_id, []).append(file)

    # ---- 企业 ------------------------------------------------------------
    companies = list(
        db.scalars(
            select(Company).where(
                Company.deleted_at.is_(None), Company.project_id.in_(project_ids)
            )
        )
    ) if project_ids else []
    company_counts: dict[str, int] = {}
    for company in companies:
        company_counts[company.project_id] = company_counts.get(company.project_id, 0) + 1

    company_file_rows = db.execute(
        select(OriginalFile.company_id, OriginalFile.parse_status, func.count())
        .where(
            OriginalFile.is_active.is_(True),
            OriginalFile.kind == FileKind.BID.value,
            OriginalFile.company_id.isnot(None),
            OriginalFile.project_id.in_(project_ids),
        )
        .group_by(OriginalFile.company_id, OriginalFile.parse_status)
    ).all() if project_ids else []
    parsed_company_ids: set[str] = set()
    for company_id, parse_status, count in company_file_rows:
        if parse_status in project_service.PARSED_FILE_STATUSES:
            parsed_company_ids.add(company_id)
    unconfirmed_by_project: dict[str, list[Company]] = {}
    for company in companies:
        if company.confirmed_at is None and company.id in parsed_company_ids:
            unconfirmed_by_project.setdefault(company.project_id, []).append(company)

    # ---- 每个项目最近的分析任务 -------------------------------------------
    latest_task_by_project = _latest_tasks(db, project_ids)

    # ---- 已导出报告的项目 -------------------------------------------------
    exported_projects = {
        entity_id
        for entity_id in db.scalars(
            select(AuditLog.entity_id).where(
                AuditLog.action == "report.export", AuditLog.entity_id.isnot(None)
            )
        ).all()
        if entity_id
    }

    # ---- 待办清单 ---------------------------------------------------------
    todos: list[dict] = []
    for project in projects:
        last_task = latest_task_by_project.get(project.id)
        ctx = {
            "failed_files": failed_by_project.get(project.id, []),
            "partial_files": partial_by_project.get(project.id, []),
            "unconfirmed_companies": unconfirmed_by_project.get(project.id, []),
            "last_task": last_task,
            "pending_review_count": pending_by_project.get(project.id, 0),
            "exported": project.id in exported_projects,
        }
        todos.extend(project_service.collect_todos(db, project, ctx=ctx))
    todos.sort(
        key=lambda todo: (
            SEVERITY_ORDER.get(todo["severity"], 9),
            _as_timestamp(todo["created_at"]) or 0,
        )
    )
    todos = todos[:todo_limit]

    # ---- 最近项目 ---------------------------------------------------------
    recent_projects = []
    for project in sorted(
        projects, key=lambda item: _as_timestamp(item.updated_at) or 0, reverse=True
    )[:project_limit]:
        last_task = latest_task_by_project.get(project.id)
        recent_projects.append(
            {
                "id": project.id,
                "project_name": project.project_name,
                "project_code": project.project_code,
                "status": project.status,
                "status_label": _project_status_label(project.status),
                "company_count": company_counts.get(project.id, 0),
                "file_count": sum(file_status_by_project.get(project.id, {}).values()),
                "last_analysis_at": (
                    (last_task.finished_at or last_task.created_at) if last_task else None
                ),
                "pending_review_count": pending_by_project.get(project.id, 0),
                "evidence_count": by_project.get(project.id, 0),
                "updated_at": project.updated_at,
                "link": f"#/projects/{project.id}",
            }
        )

    # ---- 最近分析任务 -----------------------------------------------------
    recent_task_rows = list(
        db.scalars(
            select(AnalysisTask)
            .where(in_projects)
            .order_by(AnalysisTask.created_at.desc())
            .limit(task_limit)
        )
    ) if project_ids else []
    project_names = {project.id: project.project_name for project in projects}
    recent_tasks = []
    for task in recent_task_rows:
        snapshot_companies = (task.company_snapshot or {}).get("companies", [])
        rule_versions = (task.analysis_config or {}).get("rule_versions") or {}
        recent_tasks.append(
            {
                "id": task.id,
                "task_no": task.task_no,
                "project_id": task.project_id,
                "project_name": project_names.get(task.project_id, ""),
                "status": task.status,
                "company_count": len(snapshot_companies),
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "algorithm_version": task.algorithm_version,
                "rule_version_label": _rule_version_label(rule_versions),
                "link": f"#/projects/{task.project_id}?tab=task&task={task.id}",
            }
        )

    # ---- 风险统计 ---------------------------------------------------------
    project_name_by_id = {project.id: project.project_name for project in projects}
    by_project_list = sorted(
        (
            {
                "project_id": project_id,
                "project_name": project_name_by_id.get(project_id, ""),
                "count": count,
            }
            for project_id, count in by_project.items()
        ),
        key=lambda item: item["count"],
        reverse=True,
    )[:10]

    return {
        "stats": {
            "project_total": len(projects),
            "project_active": sum(
                1
                for project in projects
                if project.status != ProjectStatus.ARCHIVED.value
            ),
            "task_completed": task_completed,
            "task_partial": task_partial,
            "evidence_total": evidence_total,
            "evidence_pending_review": evidence_pending,
        },
        "todos": todos,
        "recent_projects": recent_projects,
        "recent_tasks": recent_tasks,
        "risk": {
            "by_type": {
                EvidenceType.TEXT.value: by_type.get(EvidenceType.TEXT.value, 0),
                EvidenceType.PERSONNEL.value: by_type.get(EvidenceType.PERSONNEL.value, 0),
                EvidenceType.PRICE.value: by_type.get(EvidenceType.PRICE.value, 0),
            },
            "by_severity": {
                level: by_severity.get(level, 0)
                for level in [
                    RiskLevel.HIGH.value,
                    RiskLevel.MEDIUM.value,
                    RiskLevel.LOW.value,
                    RiskLevel.NONE.value,
                ]
            },
            "by_review_status": {
                status: by_review.get(status, 0)
                for status in [
                    ReviewStatus.PENDING.value,
                    ReviewStatus.CONFIRMED.value,
                    ReviewStatus.EXCLUDED.value,
                    ReviewStatus.UNDECIDED.value,
                ]
            },
            "by_project": by_project_list,
        },
    }


def _count_by(db: Session, column, *filters) -> dict:
    rows = db.execute(
        select(column, func.count()).where(*filters).group_by(column)
    ).all()
    return {key: count for key, count in rows if key is not None}


def _latest_tasks(db: Session, project_ids: list[str]) -> dict[str, AnalysisTask]:
    if not project_ids:
        return {}
    latest = dict(
        db.execute(
            select(AnalysisTask.project_id, func.max(AnalysisTask.created_at))
            .where(AnalysisTask.project_id.in_(project_ids))
            .group_by(AnalysisTask.project_id)
        ).all()
    )
    if not latest:
        return {}
    tasks = db.scalars(
        select(AnalysisTask).where(
            AnalysisTask.project_id.in_(project_ids),
            AnalysisTask.created_at.in_(list(latest.values())),
        )
    ).all()
    result: dict[str, AnalysisTask] = {}
    for task in tasks:
        current = result.get(task.project_id)
        if current is None or task.created_at > current.created_at:
            result[task.project_id] = task
    return result


def _rule_version_label(rule_versions: dict) -> str:
    if not rule_versions:
        return "—"
    parts = []
    for dimension, entry in sorted(rule_versions.items()):
        parts.append(f"{dimension} v{entry.get('version_no', '?')}")
    return " · ".join(parts)


def _project_status_label(status: str) -> str:
    return {
        ProjectStatus.PENDING.value: "准备中",
        ProjectStatus.READY.value: "准备中",
        ProjectStatus.ANALYZING.value: "分析中",
        ProjectStatus.ANALYZED.value: "分析完成",
        ProjectStatus.ARCHIVED.value: "已归档",
    }.get(status, status)


def _as_timestamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()
