from __future__ import annotations

from collections import Counter, defaultdict
from io import BytesIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AnalysisTask,
    ComparisonEvidence,
    ComparisonPair,
    EvidenceParticipant,
    Project,
    ReviewRecord,
    User,
)
from app.models.enums import EvidenceType, ReviewStatus, RiskLevel, TaskStatus
from app.services import audit

RISK_LABELS = {
    RiskLevel.NONE.value: "无",
    RiskLevel.LOW.value: "低",
    RiskLevel.MEDIUM.value: "中",
    RiskLevel.HIGH.value: "高",
}
REVIEW_LABELS = {
    ReviewStatus.PENDING.value: "待审核",
    ReviewStatus.CONFIRMED.value: "确认异常线索",
    ReviewStatus.EXCLUDED.value: "正常/排除",
    ReviewStatus.UNDECIDED.value: "暂不判断",
}
TYPE_LABELS = {
    EvidenceType.TEXT.value: "文本相似",
    EvidenceType.PERSONNEL.value: "人员重复",
    EvidenceType.PRICE.value: "价格异常",
}
_RISK_ORDER = {
    RiskLevel.NONE.value: 0,
    RiskLevel.LOW.value: 1,
    RiskLevel.MEDIUM.value: 2,
    RiskLevel.HIGH.value: 3,
}


def get_task(
    db: Session,
    project_id: str,
    task_id: str | None = None,
    finished_only: bool = False,
) -> AnalysisTask | None:
    if task_id:
        task = db.get(AnalysisTask, task_id)
        if not task or task.project_id != project_id:
            return None
        if finished_only and task.status not in {
            TaskStatus.COMPLETED.value,
            TaskStatus.PARTIAL.value,
        }:
            return None
        return task
    query = select(AnalysisTask).where(AnalysisTask.project_id == project_id)
    if finished_only:
        query = query.where(
            AnalysisTask.status.in_(
                [TaskStatus.COMPLETED.value, TaskStatus.PARTIAL.value]
            )
        )
    return db.scalars(query.order_by(AnalysisTask.created_at.desc()).limit(1)).first()


def _snapshot_names(task: AnalysisTask) -> dict[str, str]:
    return {
        c["company_id"]: c["company_name"]
        for c in (task.company_snapshot or {}).get("companies", [])
    }



def _price_evidences(db: Session, task: AnalysisTask) -> list[ComparisonEvidence]:
    return list(
        db.scalars(
            select(ComparisonEvidence).where(
                ComparisonEvidence.analysis_task_id == task.id,
                ComparisonEvidence.evidence_type == EvidenceType.PRICE.value,
                ComparisonEvidence.is_current.is_(True),
            )
        )
    )



def overview(db: Session, project: Project, task_id: str | None = None) -> dict:
    task = get_task(db, project.id, task_id)
    if task is None:
        return {
            "task": None,
            "stats": {
                "participant_count": 0,
                "pair_count": 0,
                "text_evidence_count": 0,
                "personnel_duplicate_count": 0,
                "price_abnormal_company_count": 0,
                "pending_review_count": 0,
                "risk_distribution": {},
            },
            "matrix": [],
            "pairs": [],
            "top_evidences": [],
            "tasks": task_history(db, project.id),
        }

    names = _snapshot_names(task)
    pairs = list(
        db.scalars(
            select(ComparisonPair)
            .where(ComparisonPair.analysis_task_id == task.id)
            .order_by(ComparisonPair.created_at)
        )
    )
    price_evidences = _price_evidences(db, task)
    all_evidences = list(
        db.scalars(
            select(ComparisonEvidence)
            .where(
                ComparisonEvidence.analysis_task_id == task.id,
                ComparisonEvidence.is_current.is_(True),
            )
            .order_by(ComparisonEvidence.created_at.desc())
        )
    )

    counts: dict[str, Counter] = defaultdict(Counter)
    for evidence in all_evidences:
        if evidence.comparison_pair_id:
            counts[evidence.comparison_pair_id][evidence.evidence_type] += 1

    pair_rows = []
    risk_distribution: Counter = Counter()
    for pair in pairs:
        price_for_pair = [
            evidence
            for evidence in price_evidences
            if pair.company_a_id in {p.company_id for p in evidence.participants}
            and pair.company_b_id in {p.company_id for p in evidence.participants}
        ]
        row = {
            "pair_id": pair.id,
            "company_a_id": pair.company_a_id,
            "company_b_id": pair.company_b_id,
            "company_a": names.get(pair.company_a_id, pair.company_a_id),
            "company_b": names.get(pair.company_b_id, pair.company_b_id),
            "text_risk": pair.text_risk,
            "personnel_risk": pair.personnel_risk,
            "price_risk": pair.price_risk,
            "overall_risk": pair.overall_risk,
            "text_count": counts[pair.id][EvidenceType.TEXT.value],
            "personnel_count": counts[pair.id][EvidenceType.PERSONNEL.value],
            "price_count": len(price_for_pair),
            "status": pair.status,
        }
        pair_rows.append(row)
        risk_distribution[pair.overall_risk] += 1

    text_evidences = [e for e in all_evidences if e.evidence_type == EvidenceType.TEXT.value]
    personnel_evidences = [e for e in all_evidences if e.evidence_type == EvidenceType.PERSONNEL.value]
    personnel_names = set()
    for evidence in personnel_evidences:
        if evidence.evidence_data.get("name"):
            personnel_names.add(evidence.evidence_data["name"])
        if evidence.evidence_data.get("name_a"):
            personnel_names.add(evidence.evidence_data["name_a"])
        if evidence.evidence_data.get("name_b"):
            personnel_names.add(evidence.evidence_data["name_b"])

    price_companies = {
        participant.company_id
        for evidence in price_evidences
        for participant in evidence.participants
    }

    top_evidences = sorted(
        all_evidences,
        key=lambda e: (_RISK_ORDER.get(e.severity, 0), e.created_at),
        reverse=True,
    )[:10]

    top_payload = []
    for evidence in top_evidences:
        company_names = [p.company_name for p in evidence.participants]
        evidence_companies = list(dict.fromkeys(company_names))
        if evidence.comparison_pair_id:
            pair = next((p for p in pairs if p.id == evidence.comparison_pair_id), None)
            if pair:
                evidence_companies = [
                    names.get(pair.company_a_id, ""),
                    names.get(pair.company_b_id, ""),
                ]
        top_payload.append(
            {
                "id": evidence.id,
                "pair_id": evidence.comparison_pair_id,
                "evidence_type": evidence.evidence_type,
                "subtype": evidence.evidence_subtype,
                "severity": evidence.severity,
                "summary": evidence.summary,
                "review_status": evidence.review_status,
                "companies": evidence_companies,
            }
        )

    return {
        "task": {
            "id": task.id,
            "task_no": task.task_no,
            "status": task.status,
            "created_at": task.created_at,
            "finished_at": task.finished_at,
            "dimensions": task.analysis_config.get("dimensions", []),
            "algorithm_version": task.algorithm_version,
        },
        "stats": {
            "participant_count": len(names),
            "pair_count": len(pairs),
            "text_evidence_count": len(text_evidences),
            "effective_text_count": sum(
                1 for e in text_evidences if not e.evidence_data.get("excluded_by_tender")
            ),
            "personnel_duplicate_count": len(personnel_names),
            "price_abnormal_company_count": len(price_companies),
            "pending_review_count": sum(
                1 for e in all_evidences if e.review_status == ReviewStatus.PENDING.value
            ),
            "risk_distribution": {
                level: risk_distribution.get(level, 0)
                for level in ["high", "medium", "low", "none"]
            },
        },
        "matrix": pair_rows,
        "pairs": pair_rows,
        "top_evidences": top_payload,
        "tasks": task_history(db, project.id),
    }


def task_history(db: Session, project_id: str) -> list[dict]:
    tasks = list(
        db.scalars(
            select(AnalysisTask)
            .where(AnalysisTask.project_id == project_id)
            .order_by(AnalysisTask.created_at.desc())
        )
    )
    return [
        {
            "id": t.id,
            "task_no": t.task_no,
            "status": t.status,
            "created_at": t.created_at,
            "finished_at": t.finished_at,
            "algorithm_version": t.algorithm_version,
            "company_count": len((t.company_snapshot or {}).get("companies", [])),
        }
        for t in tasks
    ]


def pair_detail(db: Session, pair: ComparisonPair) -> dict:
    task = db.get(AnalysisTask, pair.analysis_task_id)
    names = _snapshot_names(task)
    evidences = [evidence for evidence in pair.evidences if evidence.is_current]
    if task:
        price_evidences = _price_evidences(db, task)
        for evidence in price_evidences:
            participant_ids = {p.company_id for p in evidence.participants}
            if {pair.company_a_id, pair.company_b_id} <= participant_ids:
                evidences.append(evidence)

    evidences.sort(key=lambda e: (_RISK_ORDER.get(e.severity, 0), e.created_at), reverse=True)
    counts = Counter(e.evidence_type for e in evidences)
    return {
        "pair": {
            "id": pair.id,
            "task_id": pair.analysis_task_id,
            "project_id": task.project_id if task else None,
            "company_a_id": pair.company_a_id,
            "company_b_id": pair.company_b_id,
            "company_a": names.get(pair.company_a_id, pair.company_a_id),
            "company_b": names.get(pair.company_b_id, pair.company_b_id),
            "text_risk": pair.text_risk,
            "personnel_risk": pair.personnel_risk,
            "price_risk": pair.price_risk,
            "overall_risk": pair.overall_risk,
            "risk_reasons": pair.risk_reasons,
            "status": pair.status,
            "error_message": pair.error_message,
        },
        "counts": {
            "text": counts[EvidenceType.TEXT.value],
            "personnel": counts[EvidenceType.PERSONNEL.value],
            "price": counts[EvidenceType.PRICE.value],
        },
        "evidences": [_evidence_summary(e) for e in evidences],
    }


def _source_summary(source: dict | None) -> dict | None:
    if not source:
        return None
    return {
        "file_id": source.get("file_id"),
        "file_name": source.get("file_name"),
        "file_type": source.get("file_type"),
        "file_version": source.get("file_version"),
        "page_index": source.get("page_index"),
        "page_start": source.get("page_start"),
        "page_end": source.get("page_end"),
        "printed_page_number": source.get("printed_page_number"),
        "block_id": source.get("block_id"),
        "chunk_id": source.get("chunk_id"),
        "chapter": source.get("chapter"),
        "section": source.get("section"),
        "content_type": source.get("content_type"),
        "start_offset": source.get("start_offset"),
        "end_offset": source.get("end_offset"),
        "ranges": source.get("ranges") or [],
        "snippet": source.get("snippet"),
        "bbox": source.get("bbox"),
        "locator_precision": source.get("locator_precision"),
    }


def _evidence_summary(evidence: ComparisonEvidence) -> dict:
    return {
        "id": evidence.id,
        "evidence_type": evidence.evidence_type,
        "subtype": evidence.evidence_subtype,
        "severity": evidence.severity,
        "summary": evidence.summary,
        "review_status": evidence.review_status,
        # 是否命中招标文件公共内容（被排除的雷同内容仍保留识别记录）
        "excluded_by_tender": bool((evidence.evidence_data or {}).get("excluded_by_tender")),
        "pair_id": evidence.comparison_pair_id,
        "source_a": _source_summary(evidence.source_a),
        "source_b": _source_summary(evidence.source_b),
        "companies": [p.company_name for p in evidence.participants],
        "created_at": evidence.created_at,
    }


def evidence_detail(db: Session, evidence: ComparisonEvidence) -> dict:
    pair = evidence.pair
    task = db.get(AnalysisTask, evidence.analysis_task_id)
    names = _snapshot_names(task) if task else {}
    return {
        "id": evidence.id,
        "project_id": evidence.project_id,
        "task_id": evidence.analysis_task_id,
        "pair_id": evidence.comparison_pair_id,
        "company_a_id": pair.company_a_id if pair else None,
        "company_b_id": pair.company_b_id if pair else None,
        "company_a": names.get(pair.company_a_id) if pair else None,
        "company_b": names.get(pair.company_b_id) if pair else None,
        "evidence_type": evidence.evidence_type,
        "evidence_type_label": TYPE_LABELS.get(evidence.evidence_type, evidence.evidence_type),
        "evidence_subtype": evidence.evidence_subtype,
        "severity": evidence.severity,
        "summary": evidence.summary,
        "evidence_data": evidence.evidence_data,
        "source_a": _source_summary(evidence.source_a),
        "source_b": _source_summary(evidence.source_b),
        "participants": [
            {
                "company_id": p.company_id,
                "company_name": p.company_name,
                "role": p.role,
                "source": _source_summary(p.source),
            }
            for p in evidence.participants
        ],
        "review_status": evidence.review_status,
        "review_records": [
            {
                "id": record.id,
                "reviewer_name": record.reviewer_name,
                "old_status": record.old_status,
                "new_status": record.new_status,
                "comment": record.comment,
                "created_at": record.created_at,
            }
            for record in evidence.review_records
        ],
        "algorithm_version": evidence.algorithm_version,
        "created_at": evidence.created_at,
    }


def apply_review(
    db: Session, evidence: ComparisonEvidence, new_status: str, comment: str | None, reviewer: User
) -> ComparisonEvidence:
    record = ReviewRecord(
        evidence_id=evidence.id,
        reviewer_id=reviewer.id,
        reviewer_name=reviewer.display_name or reviewer.username,
        old_status=evidence.review_status,
        new_status=new_status,
        comment=comment,
    )
    db.add(record)
    evidence.review_status = new_status
    audit.log_audit(
        db,
        "evidence.review",
        "comparison_evidence",
        evidence.id,
        reviewer,
        {"old_status": record.old_status, "new_status": new_status},
    )
    db.commit()
    db.refresh(evidence)
    return evidence


def list_reviews(
    db: Session,
    project_id: str,
    evidence_type: str | None = None,
    severity: str | None = None,
    review_status: str | None = None,
    company_id: str | None = None,
    task_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    query = select(ComparisonEvidence).where(
        ComparisonEvidence.project_id == project_id,
        ComparisonEvidence.is_current.is_(True),
    )
    if evidence_type:
        query = query.where(ComparisonEvidence.evidence_type == evidence_type)
    if severity:
        query = query.where(ComparisonEvidence.severity == severity)
    if review_status:
        query = query.where(ComparisonEvidence.review_status == review_status)
    if task_id:
        query = query.where(ComparisonEvidence.analysis_task_id == task_id)
    if company_id:
        query = query.where(
            ComparisonEvidence.id.in_(
                select(EvidenceParticipant.evidence_id).where(
                    EvidenceParticipant.company_id == company_id
                )
            )
        )

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        db.scalars(
            query.order_by(ComparisonEvidence.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    items = []
    for evidence in rows:
        item = _evidence_summary(evidence)
        item["pair_id"] = evidence.comparison_pair_id
        item["project_id"] = evidence.project_id
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def export_excel(
    db: Session,
    project: Project,
    task_id: str | None = None,
    pair_id: str | None = None,
) -> tuple[bytes, str]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    target_pair = None
    if pair_id:
        target_pair = db.get(ComparisonPair, pair_id)
        if target_pair is None:
            raise ValueError("企业组合不存在")
        task_id = target_pair.analysis_task_id

    task = get_task(db, project.id, task_id, finished_only=True)
    if task is None:
        raise ValueError("项目还没有可导出的分析结果")

    names = _snapshot_names(task)
    pairs = list(
        db.scalars(
            select(ComparisonPair)
            .where(ComparisonPair.analysis_task_id == task.id)
            .order_by(ComparisonPair.created_at)
        )
    )
    evidences = list(
        db.scalars(
            select(ComparisonEvidence)
            .where(
                ComparisonEvidence.analysis_task_id == task.id,
                ComparisonEvidence.is_current.is_(True),
            )
            .order_by(ComparisonEvidence.created_at)
        )
    )
    if target_pair is not None:
        # 单个企业组合导出：只保留该组合的证据，以及同时涉及两家企业的价格异常证据
        wanted = {target_pair.company_a_id, target_pair.company_b_id}
        pairs = [pair for pair in pairs if pair.id == target_pair.id]
        evidences = [
            evidence
            for evidence in evidences
            if evidence.comparison_pair_id == target_pair.id
            or (
                evidence.evidence_type == EvidenceType.PRICE.value
                and wanted <= {p.company_id for p in evidence.participants}
            )
        ]
    price_evidences = [e for e in evidences if e.evidence_type == EvidenceType.PRICE.value]
    counts: dict[str, Counter] = defaultdict(Counter)
    for evidence in evidences:
        if evidence.comparison_pair_id:
            counts[evidence.comparison_pair_id][evidence.evidence_type] += 1

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F5597")
    wrap = Alignment(wrap_text=True, vertical="top")

    def safe_cell(value):
        # 防止文本被 Excel 当作公式执行
        if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
            return "'" + value
        return value

    def write_sheet(ws, headers, rows, widths):
        ws.append(headers)
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in rows:
            ws.append([safe_cell(value) for value in row])
        for index, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(index)].width = width
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = wrap

    # 1. 投标人比对概览
    ws = wb.active
    ws.title = "投标人比对概览"
    overview_rows = []
    for index, pair in enumerate(pairs, start=1):
        price_count = sum(
            1
            for evidence in price_evidences
            if {pair.company_a_id, pair.company_b_id}
            <= {p.company_id for p in evidence.participants}
        )
        overview_rows.append(
            [
                index,
                names.get(pair.company_a_id, ""),
                names.get(pair.company_b_id, ""),
                counts[pair.id][EvidenceType.TEXT.value],
                counts[pair.id][EvidenceType.PERSONNEL.value],
                price_count,
                RISK_LABELS.get(pair.text_risk, pair.text_risk),
                RISK_LABELS.get(pair.personnel_risk, pair.personnel_risk),
                RISK_LABELS.get(pair.price_risk, pair.price_risk),
                RISK_LABELS.get(pair.overall_risk, pair.overall_risk),
                "已完成" if pair.status == "completed" else pair.status,
            ]
        )
    write_sheet(
        ws,
        [
            "序号",
            "企业A",
            "企业B",
            "文本证据数",
            "人员重复数",
            "价格异常规则数",
            "文本风险",
            "人员风险",
            "价格风险",
            "综合风险",
            "状态",
        ],
        overview_rows,
        [8, 26, 26, 12, 12, 16, 10, 10, 10, 10, 10],
    )

    # 2. 文本雷同汇总表
    ws = wb.create_sheet("文本雷同汇总表")
    text_rows = []
    text_evidences = [e for e in evidences if e.evidence_type == EvidenceType.TEXT.value]
    for index, evidence in enumerate(text_evidences, start=1):
        data = evidence.evidence_data
        source_a, source_b = evidence.source_a or {}, evidence.source_b or {}
        text_rows.append(
            [
                index,
                f"E{index:04d}",
                data.get("matched_text", ""),
                source_a.get("company_name", ""),
                source_a.get("file_name", ""),
                source_a.get("page_index", ""),
                source_b.get("company_name", ""),
                source_b.get("file_name", ""),
                source_b.get("page_index", ""),
                data.get("matched_chars", 0),
                data.get("similarity_percent", 0),
                "是" if data.get("excluded_by_tender") else "否",
                REVIEW_LABELS.get(evidence.review_status, evidence.review_status),
                "; ".join(r.comment for r in evidence.review_records if r.comment),
            ]
        )
    write_sheet(
        ws,
        [
            "序号",
            "证据编号",
            "雷同内容",
            "企业A",
            "企业A文件",
            "企业A页码",
            "企业B",
            "企业B文件",
            "企业B页码",
            "雷同字数",
            "相似指标(%)",
            "公共内容排除",
            "审核状态",
            "备注",
        ],
        text_rows,
        [8, 12, 60, 22, 20, 10, 22, 20, 10, 10, 12, 14, 14, 30],
    )

    # 3. 人员姓名重复表
    ws = wb.create_sheet("人员姓名重复表")
    person_rows = []
    for index, evidence in enumerate(
        [e for e in evidences if e.evidence_type == EvidenceType.PERSONNEL.value], start=1
    ):
        data = evidence.evidence_data
        occurrences = (data.get("occurrences_a") or []) + (data.get("occurrences_b") or [])
        name = data.get("name") or f"{data.get('name_a', '')}/{data.get('name_b', '')}"
        companies = "、".join(dict.fromkeys(o.get("company_name", "") for o in occurrences))
        files = "、".join(dict.fromkeys(o.get("file_name", "") for o in occurrences))
        pages = "、".join(
            dict.fromkeys(
                f"{o.get('file_name', '')} 第{o.get('page_index', '')}页"
                for o in occurrences
            )
        )
        roles = "、".join(sorted({o.get("role", "") for o in occurrences}))
        person_rows.append(
            [
                index,
                name,
                companies,
                roles,
                files,
                pages,
                len(occurrences),
                data.get("identity_info", ""),
                REVIEW_LABELS.get(evidence.review_status, evidence.review_status),
                data.get("note", ""),
            ]
        )
    write_sheet(
        ws,
        [
            "序号",
            "重复姓名",
            "涉及企业",
            "对应岗位",
            "对应文件",
            "对应页码",
            "出现次数",
            "身份辅助信息",
            "审核状态",
            "备注",
        ],
        person_rows,
        [8, 14, 30, 20, 30, 34, 10, 20, 14, 40],
    )

    # 4. 价格异常分析表
    ws = wb.create_sheet("价格异常分析表")
    price_rows = []
    for evidence in price_evidences:
        data = evidence.evidence_data
        rule_label = data.get("rule_label", evidence.evidence_subtype)
        for quote in data.get("quotes", []):
            source = quote.get("source") or {}
            price_rows.append(
                [
                    len(price_rows) + 1,
                    quote.get("company_name", ""),
                    quote.get("amount", ""),
                    quote.get("amount_upper", ""),
                    round((quote.get("deviation_rate") or 0) * 100, 3),
                    "是" if "sequence" in evidence.evidence_subtype else "否",
                    str(int(float(quote.get("amount") or 0)))[-3:].zfill(3),
                    rule_label,
                    RISK_LABELS.get(evidence.severity, evidence.severity),
                    f"{source.get('file_name', '')} 第{source.get('page_index', '')}页",
                    data.get("note", ""),
                ]
            )
    write_sheet(
        ws,
        [
            "序号",
            "投标人名称",
            "总报价（元）",
            "大写金额",
            "偏差率(%)",
            "是否等差序列",
            "尾数特征",
            "异常类型",
            "风险提示",
            "原文位置",
            "备注",
        ],
        price_rows,
        [8, 28, 18, 30, 12, 14, 12, 26, 10, 28, 40],
    )

    stream = BytesIO()
    wb.save(stream)
    if target_pair is not None:
        company_a = names.get(target_pair.company_a_id, target_pair.company_a_id)
        company_b = names.get(target_pair.company_b_id, target_pair.company_b_id)
        filename = f"{company_a} vs {company_b}-比对证据.xlsx"
    else:
        filename = f"{project.project_name}-分析报告.xlsx"
    return stream.getvalue(), filename
