from __future__ import annotations

from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.base import EvidenceDraft
from app.analysis.baseline import TenderBaseline
from app.analysis.chunking import TextChunk, build_chunks
from app.analysis.personnel import (
    PersonOccurrence,
    build_personnel_evidences,
    extract_people,
)
from app.analysis.price import (
    Quote,
    analyze_prices,
    build_price_evidences,
    extract_quote,
)
from app.analysis.risk import max_risk, personnel_risk, price_risk, text_risk
from app.analysis.sources import BlockRef, load_company_blocks
from app.analysis.text_similarity import (
    TextIndex,
    build_tender_baseline,
    build_text_evidences,
    match_chunks,
)
from app.analysis.thresholds import text_thresholds
from app.core.config import settings
from app.models import (
    AnalysisCompanySnapshot,
    AnalysisPairResult,
    AnalysisTask,
    AnalysisTaskLog,
    Company,
    ComparisonEvidence,
    ComparisonPair,
    EvidenceParticipant,
    OriginalFile,
    Project,
    TextSimilarityResult,
    User,
)
from app.models.base import utcnow
from app.models.enums import (
    CompanyStatus,
    Dimension,
    EvidenceType,
    FileParseStatus,
    PairStatus,
    ProjectStatus,
    ReviewStatus,
    RiskLevel,
    TaskStatus,
)
from app.schemas.analysis import StartAnalysisRequest
from app.services import audit, logical_document_service, project_service, rule_service
from app.services.project_service import FINISHED_TASK_STATUSES
from app.workers import dispatch


class AnalysisValidationError(Exception):
    def __init__(self, message: str, errors: list[dict] | None = None, warnings: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or [{"code": "invalid", "message": message}]
        self.warnings = warnings or []


PARSED_STATUSES = {FileParseStatus.SUCCESS.value, FileParseStatus.PARTIAL.value}
PENDING_STATUSES = {
    FileParseStatus.UPLOADING.value,
    FileParseStatus.PENDING.value,
    FileParseStatus.PARSING.value,
}
ALL_DIMENSIONS = [d.value for d in Dimension]


def _validation(code: str, message: str, level: str = "error") -> dict:
    return {"code": code, "message": message, "level": level}


def prepare(db: Session, project: Project, company_ids: list[str] | None = None) -> dict:
    companies = project_service.active_companies(db, project.id)
    files_by_company: dict[str, list[OriginalFile]] = {}
    for file in project_service.active_bid_files(db, project.id):
        files_by_company.setdefault(file.company_id, []).append(file)
    for files in files_by_company.values():
        files.sort(key=lambda f: (f.sort_order, f.uploaded_at))

    tender_files = project_service.active_tender_files(db, project.id)
    errors: list[dict] = []
    warnings: list[dict] = []

    company_rows = []
    default_selected: list[str] = []
    for company in companies:
        files = files_by_company.get(company.id, [])
        usable = [f for f in files if f.parse_status in PARSED_STATUSES]
        pending = [f for f in files if f.parse_status in PENDING_STATUSES]
        failed = [f for f in files if f.parse_status == FileParseStatus.FAILED.value]
        partial = [f for f in files if f.parse_status == FileParseStatus.PARTIAL.value]
        company_warnings: list[str] = []
        if pending:
            company_warnings.append(f"{len(pending)} 份文件尚未解析完成")
        if failed:
            company_warnings.append(f"{len(failed)} 份文件解析失败，将不参与分析")
        if partial:
            company_warnings.append(f"{len(partial)} 份文件部分解析成功，结果可能不完整")
        if not usable:
            company_warnings.append("没有可用于分析的文件")
        if not company.confirmed_at and usable:
            company_warnings.append("企业资料尚未人工确认")

        selectable = bool(usable) and not pending
        if selectable and company.participate_analysis and company.status in {
            CompanyStatus.ANALYZABLE.value,
            CompanyStatus.PENDING_CONFIRM.value,
        }:
            default_selected.append(company.id)

        company_rows.append(
            {
                "id": company.id,
                "company_name": company.company_name,
                "status": company.status,
                "participate_analysis": company.participate_analysis,
                "confirmed": company.confirmed_at is not None,
                "file_count": len(files),
                "usable_file_count": len(usable),
                "total_pages": sum(f.page_count for f in files),
                "selectable": selectable,
                "warnings": company_warnings,
            }
        )

    selectable_ids = {row["id"] for row in company_rows if row["selectable"]}
    if company_ids is not None:
        selected_ids = [cid for cid in company_ids if cid in selectable_ids]
    else:
        selected_ids = default_selected
        if len(selected_ids) < 2:
            selected_ids = [row["id"] for row in company_rows if row["selectable"] and row["participate_analysis"]]

    selected_companies = [c for c in companies if c.id in set(selected_ids)]
    selected_files = [f for cid in selected_ids for f in files_by_company.get(cid, []) if f.parse_status in PARSED_STATUSES]

    if len(selected_companies) < 2:
        errors.append(_validation("min_companies", "至少需要选择 2 家资料完整的投标企业"))
    for company in selected_companies:
        files = files_by_company.get(company.id, [])
        if any(f.parse_status in PENDING_STATUSES for f in files):
            errors.append(
                _validation("files_unstable", f"企业“{company.company_name}”仍有文件处于上传或解析中")
            )
        if not any(f.parse_status in PARSED_STATUSES for f in files):
            errors.append(
                _validation("no_usable_file", f"企业“{company.company_name}”没有可用于分析的文件")
            )
        failed_count = sum(
            1 for f in files if f.parse_status == FileParseStatus.FAILED.value
        )
        if failed_count:
            warnings.append(
                _validation(
                    "failed_files",
                    f"企业“{company.company_name}”有 {failed_count} 份文件解析失败，这些文件将不参与本次分析",
                    "warning",
                )
            )
        if any(f.parse_status == FileParseStatus.PARTIAL.value for f in files):
            warnings.append(
                _validation(
                    "partial_parse",
                    f"企业“{company.company_name}”存在部分解析成功的文件，继续分析可能导致结果不完整",
                    "warning",
                )
            )
    if not tender_files:
        warnings.append(
            _validation(
                "no_tender",
                "当前未上传招标文件，本次分析不执行招标文件公共内容排除",
                "info",
            )
        )

    pair_count = len(selected_companies) * (len(selected_companies) - 1) // 2
    return {
        "project_id": project.id,
        "tender": {
            "uploaded": bool(tender_files),
            "files": [
                {
                    "id": f.id,
                    "file_name": f.file_name,
                    "page_count": f.page_count,
                    "parse_status": f.parse_status,
                }
                for f in tender_files
            ],
            "exclusion_default": bool(tender_files),
        },
        "companies": company_rows,
        "selected_company_ids": [c.id for c in selected_companies],
        "dimensions": ALL_DIMENSIONS,
        "summary": {
            "participant_count": len(selected_companies),
            "file_count": len(selected_files),
            "pair_count": pair_count,
            "dimension_count": len(ALL_DIMENSIONS),
        },
        "errors": errors,
        "warnings": warnings,
        "can_start": not errors,
    }


def create_task(
    db: Session,
    project: Project,
    payload,
    actor: User | None = None,
) -> AnalysisTask:
    dimensions = payload.dimensions or ALL_DIMENSIONS
    if not dimensions:
        raise AnalysisValidationError("至少选择一个分析维度")

    running = db.scalar(
        select(AnalysisTask.id)
        .where(
            AnalysisTask.project_id == project.id,
            AnalysisTask.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
        )
        .limit(1)
    )
    if running:
        raise AnalysisValidationError("项目已有分析任务正在执行，请等待完成或取消后再启动")

    companies = project_service.active_companies(db, project.id)
    company_map = {c.id: c for c in companies}
    selected_ids = payload.company_ids or [
        c.id
        for c in companies
        if c.participate_analysis
        and c.status in {CompanyStatus.ANALYZABLE.value, CompanyStatus.PENDING_CONFIRM.value}
    ]
    selected_ids = list(dict.fromkeys(selected_ids))
    selected = [company_map[cid] for cid in selected_ids if cid in company_map]

    if len(selected) < 2:
        raise AnalysisValidationError("至少需要选择 2 家投标企业")

    files_by_company: dict[str, list[OriginalFile]] = {}
    for file in project_service.active_bid_files(db, project.id):
        files_by_company.setdefault(file.company_id, []).append(file)

    errors: list[dict] = []
    warnings: list[dict] = []
    snapshot_companies: list[dict] = []

    for company in selected:
        if not company.company_name.strip():
            errors.append(_validation("empty_name", f"企业 {company.id} 名称不能为空"))
        all_files = files_by_company.get(company.id, [])
        if any(f.parse_status in PENDING_STATUSES for f in all_files):
            errors.append(
                _validation("files_unstable", f"企业“{company.company_name}”仍有文件处于上传或解析中")
            )
        requested = None
        if payload.company_file_ids and company.id in payload.company_file_ids:
            requested = set(payload.company_file_ids[company.id])
        usable = [
            f
            for f in all_files
            if f.parse_status in PARSED_STATUSES and (requested is None or f.id in requested)
        ]
        usable.sort(key=lambda f: (f.sort_order, f.uploaded_at))
        if requested is not None:
            missing = requested - {f.id for f in usable}
            if missing:
                errors.append(
                    _validation("invalid_file", f"企业“{company.company_name}”选择了不可用的文件")
                )
        if not usable:
            errors.append(
                _validation("no_usable_file", f"企业“{company.company_name}”没有可参与分析的文件")
            )
            continue
        partial = [f for f in usable if f.parse_status == FileParseStatus.PARTIAL.value]
        if partial:
            warnings.append(
                _validation(
                    "partial_parse",
                    f"企业“{company.company_name}”有 {len(partial)} 份文件部分解析成功，继续分析可能导致结果不完整",
                    "warning",
                )
            )
        failed_files = [
            f for f in all_files if f.parse_status == FileParseStatus.FAILED.value
        ]
        if failed_files:
            warnings.append(
                _validation(
                    "failed_files",
                    f"企业“{company.company_name}”有 {len(failed_files)} 份文件解析失败，未纳入本次分析",
                    "warning",
                )
            )
        version = logical_document_service.rebuild_logical_document(db, company)
        snapshot_companies.append(
            {
                "company_id": company.id,
                "company_name": company.company_name,
                "logical_document_version_id": version.id if version else None,
                "file_ids": [f.id for f in usable],
                "files": [
                    {
                        "id": f.id,
                        "file_name": f.file_name,
                        "version": f.version,
                        "file_hash": f.file_hash,
                        "page_count": f.page_count,
                        "parse_status": f.parse_status,
                    }
                    for f in usable
                ],
                "total_pages": sum(f.page_count for f in usable),
                "complete": not partial,
            }
        )

    if errors:
        raise AnalysisValidationError("分析启动校验未通过", errors=errors, warnings=warnings)
    if warnings and not payload.confirm_partial:
        raise AnalysisValidationError(
            "存在部分解析成功的文件，需要确认后才能继续分析",
            errors=[_validation("needs_confirmation", "请确认部分解析结果后继续")],
            warnings=warnings,
        )

    tender_files = project_service.active_tender_files(db, project.id)
    tender_file_version_id = (
        tender_files[0].id if (payload.exclude_tender_public_content and tender_files) else None
    )
    if payload.exclude_tender_public_content and not tender_files:
        warnings.append(
            _validation("no_tender", "未上传招标文件，跳过公共内容排除", "info")
        )

    task_no = (
        db.scalar(
            select(AnalysisTask.task_no)
            .where(AnalysisTask.project_id == project.id)
            .order_by(AnalysisTask.task_no.desc())
            .limit(1)
        )
        or 0
    ) + 1

    pair_total = len(selected) * (len(selected) - 1) // 2
    progress = {}
    for dimension in dimensions:
        if dimension == Dimension.PRICE.value:
            progress[dimension] = {"done": 0, "total": 1, "status": "pending", "failed": 0}
        else:
            progress[dimension] = {
                "done": 0,
                "total": pair_total,
                "status": "pending",
                "failed": 0,
            }

    # 固化本次分析使用的检测规则版本（PRD 5.4 / 4.13.3）：
    # 之后即使规则被修改，历史任务重跑时仍使用启动时的这一版规则。
    rule_snapshot = rule_service.active_snapshot(db)
    thresholds = rule_service.resolve_thresholds(rule_snapshot)

    task = AnalysisTask(
        project_id=project.id,
        task_no=task_no,
        status=TaskStatus.QUEUED.value,
        analysis_config={
            "dimensions": dimensions,
            "exclude_tender_public_content": payload.exclude_tender_public_content,
            "confirm_partial": payload.confirm_partial,
            "thresholds": thresholds,
            "rule_versions": rule_snapshot,
        },
        tender_file_version_id=tender_file_version_id,
        company_snapshot={
            "companies": snapshot_companies,
            "dimensions": dimensions,
            "exclude_tender_public_content": payload.exclude_tender_public_content,
        },
        algorithm_version=settings.algorithm_version,
        progress=progress,
        created_by=actor.id if actor else None,
    )
    db.add(task)
    db.flush()

    for item in snapshot_companies:
        db.add(
            AnalysisCompanySnapshot(
                task_id=task.id,
                company_id=item["company_id"],
                company_name=item["company_name"],
                logical_document_version_id=item["logical_document_version_id"],
                file_ids=item["file_ids"],
                files=item["files"],
                total_pages=item["total_pages"],
                complete=item["complete"],
            )
        )

    for company_a, company_b in combinations(selected, 2):
        db.add(
            ComparisonPair(
                analysis_task_id=task.id,
                company_a_id=company_a.id,
                company_b_id=company_b.id,
                status=PairStatus.PENDING.value,
            )
        )

    for company in selected:
        company.confirmed_at = utcnow()
        company.participate_analysis = True
        project_service.refresh_company_status(db, company)

    project.status = ProjectStatus.ANALYZING.value
    _log(db, task, f"分析任务已创建，参与企业 {len(selected)} 家，企业组合 {pair_total} 组")
    audit.log_audit(
        db,
        "analysis.create",
        "analysis_task",
        task.id,
        actor,
        {"task_no": task_no, "companies": len(selected), "dimensions": dimensions},
    )
    db.commit()
    db.refresh(task)
    return task


def _log(db: Session, task: AnalysisTask, message: str, level: str = "info") -> None:
    db.add(AnalysisTaskLog(task_id=task.id, level=level, message=message))


def _files_signature(files: list[OriginalFile]) -> tuple:
    """参与比对的文件集合签名，用于判断是否需要重新比对。"""
    return tuple(sorted((f.company_id or "", f.id, f.version or 0) for f in files))


def _task_signature(task: AnalysisTask) -> tuple:
    """从任务快照还原文件集合签名。"""
    snapshot = task.company_snapshot or {}
    items = []
    for company in snapshot.get("companies", []):
        for item in company.get("files", []):
            items.append((company.get("company_id", ""), item.get("id", ""), item.get("version", 0)))
    return tuple(sorted(items))


def auto_start_if_ready(db: Session, project: Project, actor: User | None = None) -> AnalysisTask | None:
    """投标文件全部解析完成后自动发起两两比对。

    规则是「上传即比对」：所有解析完成、且标记为参与分析的企业全部进入比对，
    不需要人工勾选企业或分析维度；比对范围与上次任务完全一致时不重复发起。
    """
    # 锁住项目行：多份文件并行解析结束时，只允许其中一个事务真正发起比对
    db.execute(select(Project.id).where(Project.id == project.id).with_for_update())
    try:
        return _auto_start_locked(db, project, actor)
    except AnalysisValidationError:
        db.rollback()
        return None
    finally:
        # create_task 内部已提交；走到这里说明没有创建任务，需要释放行锁
        db.rollback()


def _auto_start_locked(db: Session, project: Project, actor: User | None) -> AnalysisTask | None:
    bid_files = project_service.active_bid_files(db, project.id)
    if any(f.parse_status in PENDING_STATUSES for f in bid_files):
        return None  # 还有文件处于上传/解析中，等全部解析完再比对
    usable = [f for f in bid_files if f.parse_status in PARSED_STATUSES]
    company_ids = [
        company.id
        for company in project_service.active_companies(db, project.id)
        if company.participate_analysis
        and any(f.company_id == company.id for f in usable)
    ]
    if len(company_ids) < 2:
        return None

    running = db.scalar(
        select(AnalysisTask.id)
        .where(
            AnalysisTask.project_id == project.id,
            AnalysisTask.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
        )
        .limit(1)
    )
    if running:
        return None

    signature = _files_signature(usable)
    last_task = db.scalar(
        select(AnalysisTask)
        .where(AnalysisTask.project_id == project.id)
        .order_by(AnalysisTask.task_no.desc())
        .limit(1)
    )
    if (
        last_task
        and last_task.status in FINISHED_TASK_STATUSES
        and _task_signature(last_task) == signature
    ):
        return None  # 与上次比对内容一致，无需重复发起

    request = StartAnalysisRequest(
        company_ids=company_ids,
        dimensions=list(ALL_DIMENSIONS),
        exclude_tender_public_content=True,
        confirm_partial=True,
    )
    task = create_task(db, project, request, actor)
    dispatch.enqueue_analysis(task.id)
    return task


def _set_progress(task: AnalysisTask, dimension: str, **values) -> None:
    """JSON 列的嵌套字段不会自动被 SQLAlchemy 跟踪，必须整体赋值。"""
    progress = dict(task.progress or {})
    entry = dict(progress.get(dimension) or {})
    entry.update(values)
    progress[dimension] = entry
    task.progress = progress


def _persist_text_results(
    db: Session,
    task: AnalysisTask,
    pair: ComparisonPair,
    matches: list,
) -> None:
    """按文本块对保存 Jaccard 结果（同一块对有多条局部证据时聚合为一条）。"""
    aggregated: dict[tuple[str, str], dict] = {}
    for match in matches:
        key = (match.chunk_a.chunk_id, match.chunk_b.chunk_id)
        entry = aggregated.get(key)
        if entry is None:
            aggregated[key] = {
                "match": match,
                "matched_chars": match.matched_chars,
                "max_local_ratio": match.local_ratio,
                "excluded_by_tender": match.excluded_by_tender,
            }
            continue
        entry["matched_chars"] = max(entry["matched_chars"], match.matched_chars)
        entry["max_local_ratio"] = max(entry["max_local_ratio"], match.local_ratio)
        entry["excluded_by_tender"] = entry["excluded_by_tender"] or match.excluded_by_tender

    for entry in aggregated.values():
        match = entry["match"]
        db.add(
            TextSimilarityResult(
                analysis_task_id=task.id,
                pair_id=pair.id,
                company_a_id=pair.company_a_id,
                company_b_id=pair.company_b_id,
                chunk_a_id=match.chunk_a.chunk_id,
                chunk_b_id=match.chunk_b.chunk_id,
                file_a_id=match.chunk_a.file_id,
                file_b_id=match.chunk_b.file_id,
                jaccard_similarity=match.similarity,
                effective_similarity=match.effective_similarity,
                containment=match.containment,
                intersection_size=match.intersection_size,
                union_size=match.union_size,
                effective_intersection_size=match.effective_intersection_size,
                effective_union_size=match.effective_union_size,
                baseline_ratio=match.baseline_ratio,
                matched_chars=entry["matched_chars"],
                max_local_ratio=entry["max_local_ratio"],
                excluded_by_tender=entry["excluded_by_tender"],
                localization_status=match.localization_status,
                algorithm_version=settings.algorithm_version,
            )
        )


def _draft_to_evidence(
    db: Session,
    task: AnalysisTask,
    pair: ComparisonPair | None,
    draft: EvidenceDraft,
    participants: list[tuple[str, str, str, dict | None]],
) -> ComparisonEvidence:
    evidence = ComparisonEvidence(
        project_id=task.project_id,
        analysis_task_id=task.id,
        comparison_pair_id=pair.id if pair else None,
        evidence_type=draft.evidence_type,
        evidence_subtype=draft.subtype,
        severity=draft.severity,
        summary=draft.summary,
        evidence_data=draft.evidence_data,
        source_a=draft.source_a,
        source_b=draft.source_b,
        algorithm_version=settings.algorithm_version,
        review_status=ReviewStatus.PENDING.value,
    )
    db.add(evidence)
    db.flush()
    for company_id, company_name, role, source in participants:
        db.add(
            EvidenceParticipant(
                evidence_id=evidence.id,
                company_id=company_id,
                company_name=company_name,
                role=role,
                source=source,
            )
        )
    return evidence


def _blocks_for_snapshot(db: Session, company: dict) -> list[BlockRef]:
    return load_company_blocks(
        db,
        company["company_id"],
        company["company_name"],
        company["file_ids"],
        limit=settings.max_blocks_per_company,
    )


def _prepare_text_engine(
    db: Session,
    task: AnalysisTask,
    blocks_by_company: dict[str, list[BlockRef]],
    thresholds: dict,
) -> tuple[dict, dict[str, list[TextChunk]], TextIndex, TenderBaseline | None]:
    """构建本次任务复用的分块、MinHash/LSH 索引和招标文件基准。"""
    rules = text_thresholds(thresholds)
    chunks_by_company = {
        company_id: build_chunks(blocks, rules)
        for company_id, blocks in blocks_by_company.items()
    }
    index = TextIndex(rules)
    index.add_chunks(
        [chunk for chunks in chunks_by_company.values() for chunk in chunks]
    )

    tender_baseline = None
    snapshot = task.company_snapshot or {}
    if task.tender_file_version_id and snapshot.get("exclude_tender_public_content"):
        tender_blocks = load_company_blocks(
            db, "tender", "招标文件", [task.tender_file_version_id], limit=10000
        )
        if tender_blocks:
            tender_baseline = build_tender_baseline(
                build_chunks(tender_blocks, rules), thresholds=thresholds
            )
    return rules, chunks_by_company, index, tender_baseline


def run_task(db: Session, task_id: str) -> AnalysisTask:
    task = db.get(AnalysisTask, task_id)
    if task is None:
        raise ValueError("分析任务不存在")
    if task.status == TaskStatus.COMPLETED.value:
        return task
    if task.status == TaskStatus.RUNNING.value and not settings.allow_rerun_running_tasks:
        return task
    if task.status == TaskStatus.CANCELLED.value:
        return task

    recovering = task.status == TaskStatus.RUNNING.value
    task.status = TaskStatus.RUNNING.value
    task.started_at = utcnow()
    task.error_message = None
    dimensions = task.analysis_config.get("dimensions", ALL_DIMENSIONS)
    # 本次分析使用的阈值快照（来自启动时生效的检测规则版本）
    task_thresholds = task.analysis_config.get("thresholds") or {}

    # 重跑时清理本任务旧结果，避免产生重复证据
    for evidence in list(
        db.scalars(
            select(ComparisonEvidence).where(
                ComparisonEvidence.analysis_task_id == task.id,
                ComparisonEvidence.is_current.is_(True),
            )
        )
    ):
        evidence.is_current = False
    for result in list(
        db.scalars(
            select(AnalysisPairResult).where(
                AnalysisPairResult.pair_id.in_(
                    select(ComparisonPair.id).where(ComparisonPair.analysis_task_id == task.id)
                )
            )
        )
    ):
        db.delete(result)
    for text_result in list(
        db.scalars(
            select(TextSimilarityResult).where(
                TextSimilarityResult.analysis_task_id == task.id
            )
        )
    ):
        db.delete(text_result)
    for pair in db.scalars(
        select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id)
    ):
        pair.status = PairStatus.PENDING.value
        pair.text_risk = RiskLevel.NONE.value
        pair.personnel_risk = RiskLevel.NONE.value
        pair.price_risk = RiskLevel.NONE.value
        pair.overall_risk = RiskLevel.NONE.value
        pair.risk_reasons = []
        pair.error_message = None
    for dimension in dimensions:
        _set_progress(
            task,
            dimension,
            done=0,
            status="running",
            failed=0,
            message=None,
        )
    if recovering:
        _log(db, task, "检测到上次中断的 running 任务，开发模式重新执行", "warning")
    _log(db, task, "分析任务开始执行")
    db.commit()

    project = db.get(Project, task.project_id)
    snapshot = task.company_snapshot or {}
    companies = snapshot.get("companies", [])

    blocks_by_company: dict[str, list[BlockRef]] = {}
    people_by_company: dict[str, list[PersonOccurrence]] = {}
    try:
        for company in companies:
            blocks = _blocks_for_snapshot(db, company)
            blocks_by_company[company["company_id"]] = blocks
            if Dimension.PERSONNEL.value in dimensions:
                people_by_company[company["company_id"]] = extract_people(blocks)
            _log(
                db,
                task,
                f"已加载 {company['company_name']} 的 {len(blocks)} 个文本文块",
            )
        db.commit()

        # 结构化分块 + 项目级 MinHash/LSH 索引只构建一次，所有企业组合复用
        if Dimension.TEXT.value in dimensions:
            text_rules, chunks_by_company, text_index, tender_baseline = _prepare_text_engine(
                db, task, blocks_by_company, task_thresholds
            )
        else:
            text_rules, chunks_by_company, text_index, tender_baseline = (
                text_thresholds(task_thresholds),
                {},
                None,
                None,
            )
        if Dimension.TEXT.value in dimensions:
            for company in companies:
                _log(
                    db,
                    task,
                    f"{company['company_name']} 生成 {len(chunks_by_company.get(company['company_id'], []))} 个比较文本块",
                )
            params = text_index.lsh_params
            _log(
                db,
                task,
                f"文本索引已建立：{len(text_index.chunks)} 个文本块，"
                f"MinHash num_perm={params['num_perm']}，"
                f"LSH bands={params['bands']} rows={params['rows']} threshold={params['threshold']}",
            )
            if tender_baseline:
                _log(
                    db,
                    task,
                    f"已建立招标文件公共内容基准（{len(tender_baseline.hashes)} 个指纹）",
                )

        pairs = list(
            db.scalars(
                select(ComparisonPair)
                .where(ComparisonPair.analysis_task_id == task.id)
                .order_by(ComparisonPair.created_at)
            )
        )
        failed_pairs = 0
        for pair in pairs:
            db.refresh(task)
            if task.status == TaskStatus.CANCELLED.value:
                return task
            try:
                _run_pair(
                    db,
                    task,
                    pair,
                    people_by_company,
                    chunks_by_company,
                    text_index,
                    tender_baseline,
                    text_rules,
                )
                db.commit()
            except Exception as exc:  # 单个企业组合失败不影响其他组合
                db.rollback()
                failed_pairs += 1
                pair = db.get(ComparisonPair, pair.id)
                if pair:
                    pair.status = PairStatus.FAILED.value
                    pair.error_message = str(exc)
                task = db.get(AnalysisTask, task_id)
                _log(db, task, f"企业组合分析失败: {exc}", "error")
                _bump_progress(task, dimensions, failed=True)
                db.commit()

        price_failed = False
        if Dimension.PRICE.value in dimensions:
            try:
                _run_price_stage(db, task, companies, blocks_by_company)
                _set_progress(task, Dimension.PRICE.value, done=1, status="completed")
                db.commit()
            except Exception as exc:
                db.rollback()
                price_failed = True
                task = db.get(AnalysisTask, task_id)
                if task:
                    _set_progress(
                        task, Dimension.PRICE.value, status="failed", message=str(exc)
                    )
                    _log(db, task, f"价格异常分析失败: {exc}", "error")
                    db.commit()

        task = db.get(AnalysisTask, task_id)
        _apply_price_risk_to_pairs(db, task)
        db.commit()

        for dimension in dimensions:
            entry = (task.progress or {}).get(dimension) or {}
            if entry.get("status") == "running":
                _set_progress(task, dimension, status="completed")
        task.finished_at = utcnow()
        total_pairs = len(pairs)
        if total_pairs and failed_pairs >= total_pairs:
            task.status = TaskStatus.FAILED.value
            task.error_message = "全部企业组合分析失败"
        elif failed_pairs or price_failed:
            task.status = TaskStatus.PARTIAL.value
            task.error_message = f"{failed_pairs} 个企业组合失败" + ("，价格异常分析失败" if price_failed else "")
        else:
            task.status = TaskStatus.COMPLETED.value
        _log(db, task, f"分析任务结束：{task.status}")
        if project:
            project_service.refresh_project_status(db, project)
        audit.log_audit(
            db,
            "analysis.finish",
            "analysis_task",
            task.id,
            None,
            {"status": task.status, "failed_pairs": failed_pairs},
        )
        db.commit()
        return task
    except Exception as exc:
        db.rollback()
        task = db.get(AnalysisTask, task_id)
        if task:
            task.status = TaskStatus.FAILED.value
            task.error_message = str(exc)
            task.finished_at = utcnow()
            _log(db, task, f"分析任务异常终止: {exc}", "error")
            if project:
                project_service.refresh_project_status(db, project)
            db.commit()
        raise


def _bump_progress(
    task: AnalysisTask,
    dimensions: list[str],
    failed: bool = False,
    record_progress: bool = True,
) -> None:
    if not record_progress:
        return
    progress = dict(task.progress or {})
    for dimension in dimensions:
        if dimension == Dimension.PRICE.value or dimension not in progress:
            continue
        entry = dict(progress.get(dimension) or {})
        entry["done"] = entry.get("done", 0) + 1
        if failed:
            entry["failed"] = entry.get("failed", 0) + 1
        entry["status"] = "running"
        progress[dimension] = entry
    task.progress = progress


def _run_pair(
    db: Session,
    task: AnalysisTask,
    pair: ComparisonPair,
    people_by_company: dict[str, list[PersonOccurrence]],
    chunks_by_company: dict[str, list[TextChunk]],
    text_index: TextIndex | None,
    tender_baseline: TenderBaseline | None,
    text_rules: dict,
    record_progress: bool = True,
) -> None:
    dimensions = task.analysis_config.get("dimensions", ALL_DIMENSIONS)
    # 本次分析使用的阈值快照（来自启动时生效的检测规则版本）
    thresholds = task.analysis_config.get("thresholds") or {}
    snapshot = {c["company_id"]: c for c in task.company_snapshot.get("companies", [])}
    company_a = snapshot.get(pair.company_a_id, {"company_name": pair.company_a_id})
    company_b = snapshot.get(pair.company_b_id, {"company_name": pair.company_b_id})

    pair.status = PairStatus.RUNNING.value
    pair.error_message = None
    db.flush()

    text_level = RiskLevel.NONE.value
    personnel_level = RiskLevel.NONE.value
    risk_reasons: list[str] = []

    if Dimension.TEXT.value in dimensions:
        started = utcnow()
        chunks_a = chunks_by_company.get(pair.company_a_id, [])
        chunks_b = chunks_by_company.get(pair.company_b_id, [])
        candidates = (
            text_index.candidates_between(chunks_a, chunks_b) if text_index is not None else []
        )
        matches, match_stats = match_chunks(
            candidates, tender_baseline, thresholds=thresholds
        )
        if match_stats["candidate_count"] >= 5000:
            _log(
                db,
                task,
                f"{company_a['company_name']} ↔ {company_b['company_name']} "
                f"候选文本块 {match_stats['candidate_count']} 对，已按 LSH 命中数排序并截断",
                "warning",
            )
        _persist_text_results(db, task, pair, matches)
        drafts = build_text_evidences(matches, tender_baseline, thresholds=thresholds)
        excluded = [d for d in drafts if d.evidence_data.get("excluded_by_tender")]
        effective = [d for d in drafts if not d.evidence_data.get("excluded_by_tender")]
        max_similarity = max(
            (
                d.evidence_data.get("effective_similarity", d.evidence_data["similarity"])
                for d in effective
            ),
            default=0.0,
        )
        for draft in drafts:
            _draft_to_evidence(
                db,
                task,
                pair,
                draft,
                [
                    (pair.company_a_id, company_a["company_name"], "a", draft.source_a),
                    (pair.company_b_id, company_b["company_name"], "b", draft.source_b),
                ],
            )
        text_level, text_reasons = text_risk(len(effective), max_similarity, thresholds)
        risk_reasons.extend(text_reasons)
        pair.text_risk = text_level
        db.add(
            AnalysisPairResult(
                pair_id=pair.id,
                dimension=Dimension.TEXT.value,
                status="completed",
                metrics={
                    "candidate_count": match_stats["candidate_count"],
                    "local_match_count": match_stats["local_match_count"],
                    "block_level_count": match_stats["block_level_count"],
                    "evidence_count": len(drafts),
                    "effective_count": len(effective),
                    "excluded_count": len(excluded),
                    "max_similarity": max_similarity,
                    "chunk_count_a": len(chunks_a),
                    "chunk_count_b": len(chunks_b),
                    "shingle_size": int(text_rules["text_shingle_size"]),
                    "minhash_num_perm": int(text_rules["text_minhash_num_perm"]),
                    "lsh_threshold": float(text_rules["text_lsh_threshold"]),
                },
                algorithm_version=settings.algorithm_version,
                started_at=started,
                finished_at=utcnow(),
            )
        )

    if Dimension.PERSONNEL.value in dimensions:
        started = utcnow()
        drafts = build_personnel_evidences(
            people_by_company.get(pair.company_a_id, []),
            people_by_company.get(pair.company_b_id, []),
            thresholds=thresholds,
        )
        for draft in drafts:
            _draft_to_evidence(
                db,
                task,
                pair,
                draft,
                [
                    (pair.company_a_id, company_a["company_name"], "a", draft.source_a),
                    (pair.company_b_id, company_b["company_name"], "b", draft.source_b),
                ],
            )
        exact = sum(1 for d in drafts if d.subtype == "exact_name")
        similar = sum(1 for d in drafts if d.subtype == "similar_name")
        personnel_level, personnel_reasons = personnel_risk(exact, similar, thresholds)
        risk_reasons.extend(personnel_reasons)
        pair.personnel_risk = personnel_level
        db.add(
            AnalysisPairResult(
                pair_id=pair.id,
                dimension=Dimension.PERSONNEL.value,
                status="completed",
                metrics={"evidence_count": len(drafts), "exact_count": exact, "similar_count": similar},
                algorithm_version=settings.algorithm_version,
                started_at=started,
                finished_at=utcnow(),
            )
        )

    pair.overall_risk = max_risk(pair.text_risk, pair.personnel_risk, pair.price_risk)
    pair.risk_reasons = risk_reasons
    pair.status = PairStatus.COMPLETED.value
    _bump_progress(task, dimensions, record_progress=record_progress)
    _log(db, task, f"{company_a['company_name']} ↔ {company_b['company_name']} 比对完成")
    db.flush()


def _run_price_stage(
    db: Session,
    task: AnalysisTask,
    companies: list[dict],
    blocks_by_company: dict[str, list[BlockRef]],
) -> None:
    quotes: list[Quote] = []
    for company in companies:
        quote = extract_quote(
            company["company_id"],
            company["company_name"],
            blocks_by_company.get(company["company_id"], []),
        )
        if quote:
            quotes.append(quote)
    if not quotes:
        _log(db, task, "未识别到结构化投标总报价，价格异常分析无结果", "warning")
        return

    findings = analyze_prices(
        quotes, thresholds=task.analysis_config.get("thresholds") or {}
    )
    if not findings:
        _log(db, task, f"识别到 {len(quotes)} 家总报价，未触发价格异常规则")
        return
    mean = sum(q.amount for q in quotes) / len(quotes)
    drafts = build_price_evidences(findings, mean)
    for draft in drafts:
        participants = [
            (
                quote["company_id"],
                quote["company_name"],
                "participant",
                quote["source"],
            )
            for quote in draft.evidence_data.get("quotes", [])
        ]
        _draft_to_evidence(db, task, None, draft, participants)
    _log(db, task, f"价格异常分析完成，触发 {len(findings)} 条规则")
    db.flush()


def _apply_price_risk_to_pairs(db: Session, task: AnalysisTask) -> None:
    price_evidences = list(
        db.scalars(
            select(ComparisonEvidence).where(
                ComparisonEvidence.analysis_task_id == task.id,
                ComparisonEvidence.evidence_type == EvidenceType.PRICE.value,
                ComparisonEvidence.is_current.is_(True),
            )
        )
    )
    coverage: list[tuple[set[str], str]] = []
    for evidence in price_evidences:
        company_ids = {
            p.company_id
            for p in evidence.participants
        }
        coverage.append((company_ids, evidence.severity))

    pairs = list(
        db.scalars(select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id))
    )
    for pair in pairs:
        severities = [
            severity
            for company_ids, severity in coverage
            if {pair.company_a_id, pair.company_b_id} <= company_ids
        ]
        level, reasons = price_risk(severities)
        pair.price_risk = level
        pair.overall_risk = max_risk(pair.text_risk, pair.personnel_risk, pair.price_risk)
        existing = [
            reason
            for reason in (pair.risk_reasons or [])
            if not reason.startswith("涉及") and "价格异常规则" not in reason
        ]
        pair.risk_reasons = existing + reasons


def retry_pair(db: Session, task: AnalysisTask, pair: ComparisonPair) -> ComparisonPair:
    if task.status == TaskStatus.RUNNING.value:
        raise AnalysisValidationError("任务正在执行中，不能重试单个企业组合")
    snapshot = {c["company_id"]: c for c in task.company_snapshot.get("companies", [])}
    companies = list(snapshot.values())
    blocks_by_company = {c["company_id"]: _blocks_for_snapshot(db, c) for c in companies}
    people_by_company = {
        company_id: extract_people(blocks)
        for company_id, blocks in blocks_by_company.items()
    }
    thresholds = task.analysis_config.get("thresholds") or {}
    if Dimension.TEXT.value in task.analysis_config.get("dimensions", ALL_DIMENSIONS):
        text_rules, chunks_by_company, text_index, tender_baseline = _prepare_text_engine(
            db, task, blocks_by_company, thresholds
        )
    else:
        text_rules, chunks_by_company, text_index, tender_baseline = (
            text_thresholds(thresholds),
            {},
            None,
            None,
        )

    # 保留历史证据与执行记录，只将旧证据标记为非当前，避免直接覆盖历史
    for evidence in list(pair.evidences):
        evidence.is_current = False
    for text_result in list(
        db.scalars(select(TextSimilarityResult).where(TextSimilarityResult.pair_id == pair.id))
    ):
        db.delete(text_result)
    pair.status = PairStatus.PENDING.value
    pair.text_risk = RiskLevel.NONE.value
    pair.personnel_risk = RiskLevel.NONE.value
    pair.price_risk = RiskLevel.NONE.value
    pair.overall_risk = RiskLevel.NONE.value
    pair.risk_reasons = []
    db.flush()

    _run_pair(
        db,
        task,
        pair,
        people_by_company,
        chunks_by_company,
        text_index,
        tender_baseline,
        text_rules,
        record_progress=False,
    )
    _apply_price_risk_to_pairs(db, task)

    pairs = list(
        db.scalars(select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id))
    )
    if all(p.status == PairStatus.COMPLETED.value for p in pairs):
        task.status = TaskStatus.COMPLETED.value
        task.error_message = None
        task.finished_at = utcnow()
    _log(db, task, f"企业组合 {pair.id} 已重新执行")
    db.commit()
    db.refresh(pair)
    return pair


def cancel_task(db: Session, task: AnalysisTask, actor: User | None = None) -> AnalysisTask:
    if task.status in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
        return task
    task.status = TaskStatus.CANCELLED.value
    task.finished_at = utcnow()
    _log(db, task, "用户取消分析任务", "warning")
    project = db.get(Project, task.project_id)
    if project:
        project_service.refresh_project_status(db, project)
    audit.log_audit(db, "analysis.cancel", "analysis_task", task.id, actor)
    db.commit()
    return task


def task_detail(db: Session, task: AnalysisTask) -> dict:
    pairs = list(
        db.scalars(select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id))
    )
    names = {
        c["company_id"]: c["company_name"]
        for c in (task.company_snapshot or {}).get("companies", [])
    }
    logs = list(
        db.scalars(
            select(AnalysisTaskLog)
            .where(AnalysisTaskLog.task_id == task.id)
            .order_by(AnalysisTaskLog.created_at)
        )
    )
    return {
        "id": task.id,
        "project_id": task.project_id,
        "task_no": task.task_no,
        "status": task.status,
        "analysis_config": task.analysis_config,
        "dimensions": task.analysis_config.get("dimensions", []),
        "company_snapshot": task.company_snapshot,
        "companies": [
            {
                "company_id": s.company_id,
                "company_name": s.company_name,
                "file_count": len(s.file_ids or []),
                "total_pages": s.total_pages,
                "complete": s.complete,
            }
            for s in task.company_snapshots
        ],
        "algorithm_version": task.algorithm_version,
        "progress": task.progress,
        "pair_count": len(pairs),
        "completed_pair_count": sum(1 for p in pairs if p.status == PairStatus.COMPLETED.value),
        "failed_pair_count": sum(1 for p in pairs if p.status == PairStatus.FAILED.value),
        "pairs": [
            {
                "id": pair.id,
                "company_a": names.get(pair.company_a_id, pair.company_a_id),
                "company_b": names.get(pair.company_b_id, pair.company_b_id),
                "status": pair.status,
                "overall_risk": pair.overall_risk,
                "error_message": pair.error_message,
            }
            for pair in pairs
        ],
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "created_at": task.created_at,
        "error_message": task.error_message,
        "logs": [
            {"level": log.level, "message": log.message, "created_at": log.created_at}
            for log in logs
        ],
    }
