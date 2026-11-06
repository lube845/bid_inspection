from datetime import datetime, timezone

from sqlalchemy import func, select
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
    CompanyStatus,
    FileKind,
    FileParseStatus,
    ProjectStatus,
    ReviewStatus,
    TaskStatus,
)

ACTIVE_TASK_STATUSES = {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}
FINISHED_TASK_STATUSES = {TaskStatus.COMPLETED.value, TaskStatus.PARTIAL.value}
ANALYZABLE_COMPANY_STATUSES = {
    CompanyStatus.ANALYZABLE.value,
    CompanyStatus.PENDING_CONFIRM.value,
}
PARSED_FILE_STATUSES = {FileParseStatus.SUCCESS.value, FileParseStatus.PARTIAL.value}
UNFINISHED_FILE_STATUSES = {
    FileParseStatus.UPLOADING.value,
    FileParseStatus.PENDING.value,
    FileParseStatus.PARSING.value,
}


def active_companies(db: Session, project_id: str) -> list[Company]:
    return list(
        db.scalars(
            select(Company)
            .where(Company.project_id == project_id, Company.deleted_at.is_(None))
            .order_by(Company.sort_order, Company.created_at)
        )
    )


def active_bid_files(db: Session, project_id: str) -> list[OriginalFile]:
    return list(
        db.scalars(
            select(OriginalFile).where(
                OriginalFile.project_id == project_id,
                OriginalFile.kind == FileKind.BID.value,
                OriginalFile.is_active.is_(True),
            )
        )
    )


def active_tender_files(db: Session, project_id: str) -> list[OriginalFile]:
    return list(
        db.scalars(
            select(OriginalFile)
            .where(
                OriginalFile.project_id == project_id,
                OriginalFile.kind == FileKind.TENDER.value,
                OriginalFile.is_active.is_(True),
            )
            .order_by(OriginalFile.uploaded_at)
        )
    )


def project_stats(db: Session, project: Project) -> dict:
    companies = active_companies(db, project.id)
    bid_files = active_bid_files(db, project.id)
    tender_files = active_tender_files(db, project.id)

    parsed_files = [f for f in bid_files if f.parse_status in PARSED_FILE_STATUSES]
    problem_files = [
        f for f in bid_files if f.parse_status in {FileParseStatus.FAILED.value, FileParseStatus.PARTIAL.value}
    ]
    pending_files = [f for f in bid_files if f.parse_status in UNFINISHED_FILE_STATUSES]
    analyzable_companies = [
        c for c in companies if c.status in ANALYZABLE_COMPANY_STATUSES and c.participate_analysis
    ]
    abnormal_companies = [c for c in companies if c.status == CompanyStatus.ABNORMAL.value]

    last_task = db.scalars(
        select(AnalysisTask)
        .where(AnalysisTask.project_id == project.id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    ).first()

    tasks_total = db.scalar(
        select(func.count()).select_from(AnalysisTask).where(AnalysisTask.project_id == project.id)
    ) or 0

    stages = {
        "tender": {
            "uploaded": bool(tender_files),
            "file_count": len(tender_files),
        },
        "companies": {
            "total": len(companies),
            "analyzable": len(analyzable_companies),
            "abnormal": len(abnormal_companies),
            "pending_confirm": sum(
                1 for c in companies if c.status == CompanyStatus.PENDING_CONFIRM.value
            ),
        },
        "files": {
            "total": len(bid_files),
            "parsed": len(parsed_files),
            "pending": len(pending_files),
            "partial": sum(1 for f in bid_files if f.parse_status == FileParseStatus.PARTIAL.value),
            "failed": sum(1 for f in bid_files if f.parse_status == FileParseStatus.FAILED.value),
            "pages": sum(f.page_count for f in bid_files),
        },
        "analysis": {
            "started": tasks_total > 0,
            "task_count": tasks_total,
            "last_status": last_task.status if last_task else None,
            "last_task_id": last_task.id if last_task else None,
            "last_analysis_at": last_task.finished_at or last_task.created_at if last_task else None,
        },
    }

    todo = summarize_todo(
        {
            "companies": companies,
            "bid_files": bid_files,
            "pending_files": pending_files,
            "problem_files": problem_files,
            "analyzable_companies": analyzable_companies,
            "last_task": last_task,
        }
    )

    return {
        "company_count": len(companies),
        "company_limit": 20,
        "file_count": len(bid_files),
        "parsed_file_count": len(parsed_files),
        "analyzable_company_count": len(analyzable_companies),
        "pending_issue_count": len(problem_files) + len(abnormal_companies),
        "tender_file_count": len(tender_files),
        "todo": todo,
        "stages": stages,
    }


def summarize_todo(ctx: dict) -> dict:
    companies = ctx["companies"]
    bid_files = ctx["bid_files"]
    pending_files = ctx["pending_files"]
    problem_files = ctx["problem_files"]
    analyzable = ctx["analyzable_companies"]
    last_task = ctx["last_task"]

    def item(code: str, label: str) -> dict:
        return {"code": code, "label": label}

    if not companies:
        return item("add_company", "添加投标企业")
    if not bid_files:
        return item("upload_files", "上传企业文件")
    if pending_files:
        return item("parse_files", "开始解析")
    if last_task and last_task.status in ACTIVE_TASK_STATUSES:
        return item("view_progress", "查看分析进度")
    if last_task and last_task.status in FINISHED_TASK_STATUSES:
        return item("view_results", "查看分析结果")
    if problem_files:
        return item("check_parse", "检查解析结果")
    if len(analyzable) < 2:
        return item("confirm_companies", "至少需要 2 家可分析的企业")
    return item("auto_compare", "等待系统自动开始比对")


def _as_timestamp(value: datetime | None) -> float | None:
    """兼容 SQLite（naive）与 PostgreSQL（aware）时间比较。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def collect_todos(db: Session, project: Project, ctx: dict | None = None) -> list[dict]:
    """返回该项目的全部待办事项（可能多条），供全局工作台聚合。

    与 summarize_todo 并存：
    - summarize_todo 只给项目概览页一个「下一步」建议；
    - collect_todos 给出工作台用的完整待办清单。

    ctx 可传入工作台预取的聚合数据，避免逐项目查询造成 N+1；不传时自行查询。

    待办规则（唯一真源，工作台与项目页共用）：

    | code              | 类型           | 条件                                                       | 入口          |
    |-------------------|----------------|------------------------------------------------------------|---------------|
    | parse_failed      | 解析失败       | 有有效投标文件解析失败                                      | ?tab=parse    |
    | parse_partial     | 部分解析成功   | 有有效投标文件部分成功                                      | ?tab=parse    |
    | confirm_scope     | 未确认分析范围 | ≥2 家企业文件已可用但未确认，且没有运行中的分析任务          | ?tab=files    |
    | analysis_failed   | 分析任务失败   | 最近一个分析任务失败                                        | ?tab=task     |
    | pending_review    | 证据待审核     | 项目内存在待审核证据                                        | 风险管理页    |
    | report_not_exported | 报告未导出   | 有已完成任务但没有导出过报告                                | ?tab=report   |
    """
    if ctx is None:
        ctx = _todo_context(db, project)

    todos: list[dict] = []

    def item(
        code: str,
        type_label: str,
        title: str,
        detail: str,
        severity: str,
        link: str,
        created_at=None,
        status: str = "pending",
    ) -> None:
        todos.append(
            {
                "code": code,
                "type_label": type_label,
                "project_id": project.id,
                "project_name": project.project_name,
                "title": title,
                "detail": detail,
                "created_at": created_at,
                "status": status,
                "severity": severity,
                "link": link,
                "entity_type": "project",
                "entity_id": project.id,
            }
        )

    failed_files = ctx["failed_files"]
    if failed_files:
        item(
            "parse_failed",
            "解析失败",
            f"{len(failed_files)} 份投标文件解析失败",
            "、".join(file.file_name for file in failed_files[:5]),
            "high",
            f"#/projects/{project.id}?tab=parse",
            created_at=min((file.uploaded_at for file in failed_files if file.uploaded_at), default=None),
            status="failed",
        )

    partial_files = ctx["partial_files"]
    if partial_files:
        item(
            "parse_partial",
            "部分解析成功",
            f"{len(partial_files)} 份投标文件仅部分解析成功",
            "、".join(file.file_name for file in partial_files[:5]),
            "medium",
            f"#/projects/{project.id}?tab=parse",
            created_at=min((file.uploaded_at for file in partial_files if file.uploaded_at), default=None),
        )

    last_task = ctx["last_task"]
    running = last_task is not None and last_task.status in ACTIVE_TASK_STATUSES

    unconfirmed = ctx["unconfirmed_companies"]
    if len(unconfirmed) >= 2 and not running:
        item(
            "confirm_scope",
            "尚未确认分析范围",
            f"{len(unconfirmed)} 家企业资料尚未确认",
            "、".join(company.company_name for company in unconfirmed[:5]),
            "medium",
            f"#/projects/{project.id}?tab=files",
        )

    if last_task is not None and last_task.status == TaskStatus.FAILED.value:
        item(
            "analysis_failed",
            "分析任务失败",
            f"分析任务 #{last_task.task_no} 执行失败",
            last_task.error_message or "请到分析任务页查看执行日志",
            "high",
            f"#/projects/{project.id}?tab=task",
            created_at=last_task.created_at,
            status="failed",
        )

    pending_review = ctx["pending_review_count"]
    if pending_review:
        item(
            "pending_review",
            "证据待审核",
            f"{pending_review} 条异常证据待审核",
            "请到风险管理页集中处理",
            "medium",
            f"#/risks?project_id={project.id}&review_status=pending",
        )

    finished = last_task is not None and last_task.status in FINISHED_TASK_STATUSES
    if finished and not ctx["exported"]:
        item(
            "report_not_exported",
            "报告尚未导出",
            f"分析任务 #{last_task.task_no} 已完成，报告尚未导出",
            "请到分析报告页导出 Excel 报告",
            "low",
            f"#/projects/{project.id}?tab=report",
        )

    return todos


def _todo_context(db: Session, project: Project) -> dict:
    """collect_todos 在没有预取数据时自行查询所需的聚合结果。"""
    bid_files = active_bid_files(db, project.id)
    companies = active_companies(db, project.id)
    last_task = db.scalars(
        select(AnalysisTask)
        .where(AnalysisTask.project_id == project.id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    ).first()
    pending_review = db.scalar(
        select(func.count())
        .select_from(ComparisonEvidence)
        .where(
            ComparisonEvidence.project_id == project.id,
            ComparisonEvidence.is_current.is_(True),
            ComparisonEvidence.review_status == ReviewStatus.PENDING.value,
        )
    ) or 0
    exported = bool(
        db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "report.export", AuditLog.entity_id == project.id)
        )
    )
    return {
        "failed_files": [
            f for f in bid_files if f.parse_status == FileParseStatus.FAILED.value
        ],
        "partial_files": [
            f for f in bid_files if f.parse_status == FileParseStatus.PARTIAL.value
        ],
        "unconfirmed_companies": [
            c
            for c in companies
            if c.confirmed_at is None
            and any(
                f.parse_status in PARSED_FILE_STATUSES
                for f in bid_files
                if f.company_id == c.id
            )
        ],
        "last_task": last_task,
        "pending_review_count": pending_review,
        "exported": exported,
    }


def refresh_project_status(db: Session, project: Project) -> str:
    if project.status == ProjectStatus.ARCHIVED.value:
        return project.status

    tasks = list(
        db.scalars(
            select(AnalysisTask)
            .where(AnalysisTask.project_id == project.id)
            .order_by(AnalysisTask.created_at.desc())
        )
    )
    bid_files = active_bid_files(db, project.id)
    companies = active_companies(db, project.id)

    if any(t.status in ACTIVE_TASK_STATUSES for t in tasks):
        project.status = ProjectStatus.ANALYZING.value
        return project.status

    latest_finished = next((t for t in tasks if t.status in FINISHED_TASK_STATUSES), None)
    has_parse_work = any(f.parse_status in UNFINISHED_FILE_STATUSES for f in bid_files)

    if latest_finished and not has_parse_work:
        finished_at = _as_timestamp(latest_finished.finished_at)
        changed_after_analysis = any(
            _as_timestamp(f.updated_at) is not None
            and finished_at is not None
            and _as_timestamp(f.updated_at) > finished_at
            for f in bid_files
        )
        if not changed_after_analysis:
            project.status = ProjectStatus.ANALYZED.value
            return project.status

    ready_companies = sum(
        1
        for c in companies
        if c.status in ANALYZABLE_COMPANY_STATUSES and c.participate_analysis
    )
    all_files_parsed = bool(bid_files) and all(
        f.parse_status in PARSED_FILE_STATUSES | {FileParseStatus.FAILED.value} for f in bid_files
    )
    if ready_companies >= 2 and all_files_parsed:
        project.status = ProjectStatus.READY.value
    else:
        project.status = ProjectStatus.PENDING.value
    return project.status


def refresh_company_status(db: Session, company: Company) -> str:
    files = list(
        db.scalars(
            select(OriginalFile).where(
                OriginalFile.company_id == company.id,
                OriginalFile.kind == FileKind.BID.value,
                OriginalFile.is_active.is_(True),
            )
        )
    )
    if not files:
        company.status = CompanyStatus.INCOMPLETE.value
        return company.status

    statuses = {f.parse_status for f in files}
    if statuses & {FileParseStatus.UPLOADING.value, FileParseStatus.PENDING.value}:
        company.status = CompanyStatus.PENDING_PARSE.value
    elif FileParseStatus.PARSING.value in statuses:
        company.status = CompanyStatus.PARSING.value
    elif statuses & {FileParseStatus.FAILED.value}:
        company.status = CompanyStatus.ABNORMAL.value
    elif statuses <= PARSED_FILE_STATUSES:
        if company.confirmed_at:
            company.status = CompanyStatus.ANALYZABLE.value
        else:
            company.status = CompanyStatus.PENDING_CONFIRM.value
    else:
        company.status = CompanyStatus.PENDING_PARSE.value
    return company.status


def company_summary(company: Company, files: list[OriginalFile]) -> dict:
    parsed = [f for f in files if f.parse_status in PARSED_FILE_STATUSES]
    return {
        "id": company.id,
        "project_id": company.project_id,
        "company_name": company.company_name,
        "credit_code": company.credit_code,
        "remark": company.remark,
        "status": company.status,
        "participate_analysis": company.participate_analysis,
        "confirmed": company.confirmed_at is not None,
        "file_count": len(files),
        "total_pages": sum(f.page_count for f in files),
        "parsed_file_count": len(parsed),
        "failed_file_count": sum(
            1 for f in files if f.parse_status == FileParseStatus.FAILED.value
        ),
        "partial_file_count": sum(
            1 for f in files if f.parse_status == FileParseStatus.PARTIAL.value
        ),
        "logical_document_status": (
            company.logical_document.status if company.logical_document else "not_generated"
        ),
        "created_at": company.created_at,
        "updated_at": company.updated_at,
    }

