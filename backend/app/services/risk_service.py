"""全局风险管理（PRD 4.11）。

把用户可见的全部项目里的异常证据汇总到一处，集中查看、集中审核。

设计要点：
1. 证据一律带 is_current 过滤 —— 重跑会把旧证据置为 False，不排除就会被重复计入。
2. 企业过滤、审核人员过滤都用 id.in_(子查询)，绝不 JOIN —— 一条涉及 3 家企业的
   项目级价格证据被 JOIN 就会把 total 和分页切片放大 3 倍。
3. 项目级价格证据（comparison_pair_id 为空）在全局列表里只出现一次，并带上全部
   参与企业；不像组合详情页那样把它重复挂到每个企业组合下。
4. 证据编号按 PRD 5.7 的格式计算，不落库：项目编号-任务编号-证据类型-序号。
   序号在同一 (任务, 类型) 内跨历史行一起编号，因此重跑后编号不会漂移。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import false, func, select
from sqlalchemy.orm import Session

from app.models import (
    AnalysisTask,
    Company,
    ComparisonEvidence,
    EvidenceParticipant,
    Project,
    ReviewRecord,
    User,
)
from app.models.enums import EvidenceType, ReviewStatus
from app.services import results_service

TYPE_CODES = {
    EvidenceType.TEXT.value: "TEXT",
    EvidenceType.PERSONNEL.value: "PERSONNEL",
    EvidenceType.PRICE.value: "PRICE",
}

# PRD 4.11.5：V1.0 批量操作只允许「标记为待进一步核查」，
# 明确不支持批量确认高风险线索或批量排除，避免误操作。
BATCH_ALLOWED_STATUSES = {ReviewStatus.UNDECIDED.value}
MAX_BATCH_SIZE = 200


class RiskValidationError(Exception):
    """风险管理的业务校验错误。"""


def evidence_code(
    project_code: str | None, task_no: int, evidence_type: str, seq: int
) -> str:
    """PRD 5.7：项目编号-任务编号-证据类型-序号，例 DEMO-2026-001-T03-TEXT-0001。"""
    head = (project_code or "NO-CODE").strip() or "NO-CODE"
    type_code = TYPE_CODES.get(evidence_type, str(evidence_type).upper())
    return f"{head}-T{int(task_no):02d}-{type_code}-{seq:04d}"


def list_risks(
    db: Session,
    project_id: str | None = None,
    company_id: str | None = None,
    evidence_type: str | None = None,
    severity: str | None = None,
    review_status: str | None = None,
    reviewer_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    query = select(ComparisonEvidence).where(ComparisonEvidence.is_current.is_(True))
    # 只统计未删除的项目，与工作台的项目口径保持一致
    visible_project_ids = [
        project_id
        for project_id in db.scalars(
            select(Project.id).where(Project.deleted_at.is_(None))
        ).all()
    ]
    if visible_project_ids:
        query = query.where(ComparisonEvidence.project_id.in_(visible_project_ids))
    else:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    if project_id:
        query = query.where(ComparisonEvidence.project_id == project_id)
    if evidence_type:
        query = query.where(ComparisonEvidence.evidence_type == evidence_type)
    if severity:
        query = query.where(ComparisonEvidence.severity == severity)
    if review_status:
        query = query.where(ComparisonEvidence.review_status == review_status)
    if date_from:
        query = query.where(ComparisonEvidence.created_at >= date_from)
    if date_to:
        query = query.where(ComparisonEvidence.created_at <= date_to)
    if company_id:
        query = query.where(
            ComparisonEvidence.id.in_(
                select(EvidenceParticipant.evidence_id).where(
                    EvidenceParticipant.company_id == company_id
                )
            )
        )
    if reviewer_id:
        query = query.where(
            ComparisonEvidence.id.in_(
                select(ReviewRecord.evidence_id).where(ReviewRecord.reviewer_id == reviewer_id)
            )
        )

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        db.scalars(
            query.order_by(
                ComparisonEvidence.created_at.desc(), ComparisonEvidence.id.desc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )

    items = _enrich(db, rows)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _enrich(db: Session, rows: list[ComparisonEvidence]) -> list[dict]:
    """整页批量补齐项目、任务、参与企业、审核人与证据编号（不做 N+1 查询）。"""
    if not rows:
        return []

    evidence_ids = [row.id for row in rows]
    task_ids = {row.analysis_task_id for row in rows}
    project_ids = {row.project_id for row in rows}

    tasks = {
        task.id: task
        for task in db.scalars(select(AnalysisTask).where(AnalysisTask.id.in_(task_ids)))
    }
    projects = {
        project.id: project
        for project in db.scalars(select(Project).where(Project.id.in_(project_ids)))
    }

    participants = db.scalars(
        select(EvidenceParticipant).where(
            EvidenceParticipant.evidence_id.in_(evidence_ids)
        )
    ).all()
    companies_by_evidence: dict[str, list[EvidenceParticipant]] = defaultdict(list)
    for participant in participants:
        companies_by_evidence[participant.evidence_id].append(participant)

    records = db.scalars(
        select(ReviewRecord)
        .where(ReviewRecord.evidence_id.in_(evidence_ids))
        .order_by(ReviewRecord.created_at.desc(), ReviewRecord.id.desc())
    ).all()
    latest_record: dict[str, ReviewRecord] = {}
    for record in records:
        latest_record.setdefault(record.evidence_id, record)

    sequences = _evidence_sequences(db, rows)

    items: list[dict] = []
    for row in rows:
        task = tasks.get(row.analysis_task_id)
        project = projects.get(row.project_id)
        row_participants = companies_by_evidence.get(row.id, [])
        company_names = list(dict.fromkeys(p.company_name for p in row_participants))
        company_ids = list(dict.fromkeys(p.company_id for p in row_participants))
        seq = sequences.get(row.id, 0)
        items.append(
            {
                "id": row.id,
                "evidence_code": evidence_code(
                    project.project_code if project else None,
                    task.task_no if task else 0,
                    row.evidence_type,
                    seq,
                ),
                "project_id": row.project_id,
                "project_name": project.project_name if project else "",
                "project_code": project.project_code if project else None,
                "task_id": row.analysis_task_id,
                "task_no": task.task_no if task else None,
                "evidence_type": row.evidence_type,
                "evidence_type_label": results_service.TYPE_LABELS.get(
                    row.evidence_type, row.evidence_type
                ),
                "evidence_subtype": row.evidence_subtype,
                "summary": row.summary,
                "companies": company_names,
                "company_ids": company_ids,
                "severity": row.severity,
                "severity_label": results_service.RISK_LABELS.get(
                    row.severity, row.severity
                ),
                "excluded_by_tender": bool(
                    (row.evidence_data or {}).get("excluded_by_tender")
                ),
                "review_status": row.review_status,
                "review_status_label": results_service.REVIEW_LABELS.get(
                    row.review_status, row.review_status
                ),
                "reviewer_name": (
                    latest_record[row.id].reviewer_name if row.id in latest_record else ""
                ),
                "created_at": row.created_at,
                "pair_id": row.comparison_pair_id,
                "viewer_link": _viewer_link(row),
            }
        )
    return items


def _viewer_link(evidence: ComparisonEvidence) -> str:
    """跳转到所属项目的三栏比对详情；项目级证据（无组合）跳到比对结果页。"""
    if evidence.comparison_pair_id:
        return (
            f"#/projects/{evidence.project_id}"
            f"?tab=analysis&pair={evidence.comparison_pair_id}&evidence={evidence.id}"
        )
    return f"#/projects/{evidence.project_id}?tab=results"


def _evidence_sequences(
    db: Session, rows: list[ComparisonEvidence]
) -> dict[str, int]:
    """同一 (任务, 类型) 内的证据序号。

    跨 is_current=False 的历史行一起编号，保证重跑后编号不变（可能有空洞）。
    """
    pairs = {(row.analysis_task_id, row.evidence_type) for row in rows}
    if not pairs:
        return {}
    task_ids = {task_id for task_id, _ in pairs}
    types = {evidence_type for _, evidence_type in pairs}

    # 窗口函数需要在完整分区上计算，但只把本页要用的编号取回来，
    # 避免把整份任务的证据行都传回 Python（任务可能有数万条证据）。
    windowed = (
        select(
            ComparisonEvidence.id.label("id"),
            func.row_number()
            .over(
                partition_by=(
                    ComparisonEvidence.analysis_task_id,
                    ComparisonEvidence.evidence_type,
                ),
                order_by=(ComparisonEvidence.created_at, ComparisonEvidence.id),
            )
            .label("seq"),
        )
        .where(
            ComparisonEvidence.analysis_task_id.in_(task_ids),
            ComparisonEvidence.evidence_type.in_(types),
        )
        .subquery()
    )
    page_ids = [row.id for row in rows]
    statement = select(windowed.c.id, windowed.c.seq).where(windowed.c.id.in_(page_ids))
    try:
        return {row_id: int(seq) for row_id, seq in db.execute(statement).all()}
    except Exception:
        # 旧版 SQLite 不支持窗口函数时退回 Python 编号
        return _evidence_sequences_python(db, task_ids, types)


def _evidence_sequences_python(
    db: Session, task_ids: set[str], types: set[str]
) -> dict[str, int]:
    rows = db.execute(
        select(
            ComparisonEvidence.id,
            ComparisonEvidence.analysis_task_id,
            ComparisonEvidence.evidence_type,
            ComparisonEvidence.created_at,
        )
        .where(
            ComparisonEvidence.analysis_task_id.in_(task_ids),
            ComparisonEvidence.evidence_type.in_(types),
        )
        .order_by(ComparisonEvidence.created_at, ComparisonEvidence.id)
    ).all()
    counters: dict[tuple[str, str], int] = defaultdict(int)
    sequences: dict[str, int] = {}
    for row_id, task_id, evidence_type, _ in rows:
        key = (task_id, evidence_type)
        counters[key] += 1
        sequences[row_id] = counters[key]
    return sequences


def risk_detail(db: Session, evidence: ComparisonEvidence) -> dict:
    """风险详情：复用证据详情结构，补上编号、项目、任务、审核人与跳转链接。"""
    base = results_service.evidence_detail(db, evidence)
    task = db.get(AnalysisTask, evidence.analysis_task_id)
    project = db.get(Project, evidence.project_id)
    participants = base.get("participants") or []
    files = []
    for source in [base.get("source_a"), base.get("source_b")]:
        if source and source.get("file_id"):
            files.append(
                {
                    "file_id": source.get("file_id"),
                    "file_name": source.get("file_name"),
                    "page_index": source.get("page_index"),
                    "printed_page_number": source.get("printed_page_number"),
                }
            )
    for participant in participants:
        source = participant.get("source") or {}
        if source.get("file_id"):
            files.append(
                {
                    "file_id": source.get("file_id"),
                    "file_name": source.get("file_name"),
                    "page_index": source.get("page_index"),
                    "printed_page_number": source.get("printed_page_number"),
                    "company_name": participant.get("company_name"),
                }
            )
    seq = _evidence_sequences(db, [evidence]).get(evidence.id, 0)
    latest_record = base.get("review_records") or []
    return {
        **base,
        "evidence_code": evidence_code(
            project.project_code if project else None,
            task.task_no if task else 0,
            evidence.evidence_type,
            seq,
        ),
        "project_name": project.project_name if project else "",
        "project_code": project.project_code if project else None,
        "task_no": task.task_no if task else None,
        "task_status": task.status if task else None,
        "companies": [p.get("company_name") for p in participants if p.get("company_name")],
        "reviewer_name": latest_record[0]["reviewer_name"] if latest_record else "",
        "files": files,
        "viewer_link": _viewer_link(evidence),
    }


def batch_review(
    db: Session,
    evidence_ids: list[str],
    new_status: str,
    comment: str | None,
    reviewer: User,
) -> dict:
    """批量标记审核状态。

    V1.0 只允许批量标记为「待进一步核查」，明确不支持批量确认或批量排除
    （PRD 4.11.5），避免误操作。
    """
    if new_status not in BATCH_ALLOWED_STATUSES:
        raise RiskValidationError("V1.0 只支持批量标记为待进一步核查，不支持批量确认或批量排除")
    if not evidence_ids:
        raise RiskValidationError("请先选择要处理的证据")
    if len(evidence_ids) > MAX_BATCH_SIZE:
        raise RiskValidationError(f"单次最多处理 {MAX_BATCH_SIZE} 条证据")

    rows = list(
        db.scalars(
            select(ComparisonEvidence).where(
                ComparisonEvidence.id.in_(evidence_ids),
                ComparisonEvidence.is_current.is_(True),
            )
        )
    )
    updated = 0
    skipped: list[str] = []
    for evidence in rows:
        db.add(
            ReviewRecord(
                evidence_id=evidence.id,
                reviewer_id=reviewer.id,
                reviewer_name=reviewer.display_name or reviewer.username,
                old_status=evidence.review_status,
                new_status=new_status,
                comment=comment,
            )
        )
        evidence.review_status = new_status
        updated += 1
    skipped = [eid for eid in evidence_ids if eid not in {row.id for row in rows}]
    return {"updated": updated, "skipped": skipped, "status": new_status}


def risk_filters(db: Session) -> dict:
    """筛选下拉的可选项。"""
    projects = list(db.scalars(select(Project).where(Project.deleted_at.is_(None))))
    project_ids = [project.id for project in projects]
    companies = list(
        db.scalars(
            select(Company).where(
                Company.deleted_at.is_(None),
                Company.project_id.in_(project_ids) if project_ids else false(),
            )
        )
    ) if project_ids else []
    project_names = {project.id: project.project_name for project in projects}
    reviewer_rows = db.execute(
        select(ReviewRecord.reviewer_id, ReviewRecord.reviewer_name)
        .where(ReviewRecord.reviewer_id.isnot(None))
        .group_by(ReviewRecord.reviewer_id, ReviewRecord.reviewer_name)
    ).all()
    return {
        "projects": [
            {
                "id": project.id,
                "project_name": project.project_name,
                "project_code": project.project_code,
            }
            for project in projects
        ],
        "companies": [
            {
                "company_id": company.id,
                "company_name": company.company_name,
                "project_id": company.project_id,
                "project_name": project_names.get(company.project_id, ""),
            }
            for company in companies
        ],
        "reviewers": [
            {"id": reviewer_id, "name": name}
            for reviewer_id, name in reviewer_rows
            if reviewer_id
        ],
    }
