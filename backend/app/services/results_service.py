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
    PriceQuote,
    Project,
    ReviewRecord,
    User,
)
from app.models.enums import EvidenceType, ReviewStatus, RiskLevel, TaskStatus
from app.services import audit, evidence_context

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ 自带
    ZoneInfo = None  # type: ignore[assignment]


def _display_tz():
    """报告里的时间统一换算到配置的展示时区。

    数据库存的是 UTC，页面按浏览器本地时间显示；Excel 在服务端生成，
    若不换算，同一条记录在页面与报告里会相差 8 小时（QA 首轮第 6 条）。
    """
    from app.core.config import settings

    name = getattr(settings, "display_timezone", None) or "Asia/Shanghai"
    if ZoneInfo is None:  # pragma: no cover
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - 时区库缺失时退回 UTC
        return None


def _format_display_time(value):
    """UTC datetime → 「YYYY-MM-DD HH:MM」本地时间字符串。"""
    if value is None:
        return ""
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    tz = _display_tz()
    if tz is not None:
        value = value.astimezone(tz)
    return value.strftime("%Y-%m-%d %H:%M")

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
    EvidenceType.FILE.value: "文件指纹",
    EvidenceType.METADATA.value: "文档元数据",
}
# 报价来源：报告「报价汇总表」与清单页共用同一套文案
QUOTE_SOURCE_LABELS = {
    "manual": "手工补录",
    "import": "批量导入",
    "auto": "自动识别",
}
# 取证类证据的子类型标签：Excel 报告与风险列表共用
SUBTYPE_LABELS = {
    "identical_file": "同一份文件",
    "shared_metadata": "元数据同源",
    "shared_image": "相同图片",
    "shared_rsid": "修订标识同源",
    "shared_contact": "共用联系方式",
    "unusual_duration_match": "非常规工期一致",
    "role_roster_match": "项目班子一致",
    "line_item_duplicate": "逐项单价雷同",
    "structure_similarity": "文档结构相似",
    "shared_anomaly": "同一处非规范写法",
    "amount_case_mismatch": "大小写金额不一致",
    "quote_needs_confirmation": "自动识别金额待确认",
}


def _forensics_detail(evidence: ComparisonEvidence, data: dict) -> str:
    """把不同子类型的取证证据压成一行摘要文本，供 Excel 取证表展示。"""
    subtype = evidence.evidence_subtype
    if subtype == "identical_file":
        return f"SHA-256 {data.get('file_hash', '')}"
    if subtype == "structure_similarity":
        comparison = data.get("comparison") or {}
        parts = []
        if comparison.get("title_hit"):
            parts.append(f"标题 {comparison.get('title_common', 0)} 个同序")
        if comparison.get("table_hit"):
            parts.append(f"表格 {comparison.get('table_pairs', 0)} 张同构")
        if comparison.get("image_hit"):
            parts.append(f"图片 {comparison.get('image_run', 0)} 张同序")
        return "；".join(parts) or "—"
    if subtype == "shared_anomaly":
        return f"{data.get('term', '')} → 应为「{data.get('correction', '')}」"
    if subtype == "shared_image":
        return (
            f"匹配 {data.get('match_count', 0)} 张图片，"
            f"其中完全相同 {data.get('identical_count', 0)} 张"
        )
    if subtype == "shared_rsid":
        return (
            f"共同修订标识 {data.get('shared_count', 0)} 个，"
            f"真实编辑痕迹 {data.get('shared_trace_count', 0)} 个"
        )
    if subtype == "shared_contact":
        return f"{data.get('value', '')}"
    if subtype == "unusual_duration_match":
        return f"工期 {data.get('value', '')}"
    if subtype == "role_roster_match":
        roster = data.get("roster") or {}
        return "；".join(f"{role}={name}" for role, name in roster.items())
    # shared_metadata 与兜底：列出共同字段
    labels = data.get("shared_field_labels") or {}
    shared = data.get("shared_fields") or {}
    return "；".join(f"{labels.get(key, key)}={value}" for key, value in shared.items())


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
            "forensic_risk": pair.forensic_risk,
            "overall_risk": pair.overall_risk,
            "text_count": counts[pair.id][EvidenceType.TEXT.value],
            "personnel_count": counts[pair.id][EvidenceType.PERSONNEL.value],
            # 价格证据有两类：项目级规则（报价集中/等差/尾数，挂在参与企业上）
            # 与组合级逐项单价雷同（挂在组合上）。两处口径必须合并，否则
            # 「逐项单价」证据会在价格列里凭空消失。
            "price_count": len(price_for_pair)
            + counts[pair.id][EvidenceType.PRICE.value],
            # 文件级取证：同一份文件被两家提交 / 元数据指向同一编写环境
            "file_count": counts[pair.id][EvidenceType.FILE.value],
            "metadata_count": counts[pair.id][EvidenceType.METADATA.value],
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
    # 价格维度的覆盖情况由分析阶段写入任务进度；这里原样下发给前端做显式提示。
    price_coverage = (
        (task.progress or {}).get("price") or {}
    ).get("coverage") or {}

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
            # 有效 = 没有命中招标公共内容、不是批次共同模板、也不是封面字段雷同
            "effective_text_count": sum(
                1
                for e in text_evidences
                if not e.evidence_data.get("excluded_by_tender")
                and not e.evidence_data.get("excluded_by_template")
                and not e.evidence_data.get("cover_only")
            ),
            "template_text_count": sum(
                1 for e in text_evidences if e.evidence_data.get("excluded_by_template")
            ),
            "cover_text_count": sum(
                1 for e in text_evidences if e.evidence_data.get("cover_only")
            ),
            "personnel_duplicate_count": len(personnel_names),
            "price_abnormal_company_count": len(price_companies),
            # 文件级取证：文件指纹相同 / 元数据同源
            "file_evidence_count": sum(
                1 for e in all_evidences if e.evidence_type == EvidenceType.FILE.value
            ),
            "metadata_evidence_count": sum(
                1 for e in all_evidences if e.evidence_type == EvidenceType.METADATA.value
            ),
            # 价格维度覆盖情况：识别到报价的企业数 / 应识别企业数 / 未识别的企业
            "price_coverage": price_coverage,
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


def _evidence_identity(evidence: ComparisonEvidence) -> str:
    """跨任务匹配证据的稳定键.

    文本证据用「组合 + 块对」，其余用「组合/参与企业 + 类型 + 子类型 + 关键值」。
    重跑会生成新行 id，只有内容键能跨任务对上。
    """
    data = evidence.evidence_data or {}
    company_ids = "|".join(sorted({p.company_id for p in evidence.participants}))
    # 组合 id 每次任务都会变，跨任务匹配必须用企业 id 而不是 comparison_pair_id
    pair = evidence.pair
    pair_key = (
        "|".join(sorted([pair.company_a_id, pair.company_b_id]))
        if pair
        else company_ids
    )
    if evidence.evidence_type == EvidenceType.TEXT.value:
        chunk_a = data.get("chunk_a_id") or ""
        chunk_b = data.get("chunk_b_id") or ""
        if chunk_a or chunk_b:
            return f"text|{pair_key}|{chunk_a}|{chunk_b}|{evidence.evidence_subtype}"
        return f"text|{pair_key}|{data.get('matched_text', '')[:40]}"
    if evidence.evidence_type == EvidenceType.PERSONNEL.value:
        return f"personnel|{pair_key}|{data.get('name') or data.get('name_a')}"
    if evidence.evidence_type == EvidenceType.PRICE.value:
        return f"price|{company_ids}|{evidence.evidence_subtype}"
    if evidence.evidence_subtype == "structure_similarity":
        return f"structure|{pair_key}"
    value = data.get("value") or data.get("file_hash") or data.get("term") or ""
    return f"{evidence.evidence_type}|{pair_key}|{evidence.evidence_subtype}|{value}"


def _evidence_identity_label(evidence: ComparisonEvidence) -> str:
    labels = SUBTYPE_LABELS.get(evidence.evidence_subtype)
    return labels or TYPE_LABELS.get(evidence.evidence_type, evidence.evidence_type)


def task_diff(
    db: Session,
    project: Project,
    current_task_id: str,
    previous_task_id: str | None = None,
) -> dict:
    """比较同一项目的两个分析任务：输入、证据、风险与审核结论的变化。"""
    current = db.get(AnalysisTask, current_task_id)
    if current is None or current.project_id != project.id:
        raise ValueError("分析任务不存在")
    if previous_task_id:
        previous = db.get(AnalysisTask, previous_task_id)
    else:
        previous = db.scalars(
            select(AnalysisTask)
            .where(
                AnalysisTask.project_id == project.id,
                AnalysisTask.task_no < current.task_no,
            )
            .order_by(AnalysisTask.task_no.desc())
            .limit(1)
        ).first()
    if previous is None:
        raise ValueError("没有可对比的上一次分析任务")

    def evidences_of(task: AnalysisTask) -> list[ComparisonEvidence]:
        return list(
            db.scalars(
                select(ComparisonEvidence).where(
                    ComparisonEvidence.analysis_task_id == task.id,
                    ComparisonEvidence.is_current.is_(True),
                )
            )
        )

    current_evidences = evidences_of(current)
    previous_evidences = evidences_of(previous)
    current_map = {_evidence_identity(e): e for e in current_evidences}
    previous_map = {_evidence_identity(e): e for e in previous_evidences}

    added, removed, changed = [], [], []
    for key, evidence in current_map.items():
        old = previous_map.get(key)
        if old is None:
            added.append(
                {
                    "id": evidence.id,
                    "label": _evidence_identity_label(evidence),
                    "severity": evidence.severity,
                    "summary": evidence.summary,
                    "review_status": evidence.review_status,
                }
            )
        elif old.severity != evidence.severity:
            # 只比风险等级：新任务里证据一律从 pending 开始，把审核状态重置
            # 当成「变化」会让每次对比都刷屏。
            changed.append(
                {
                    "id": evidence.id,
                    "label": _evidence_identity_label(evidence),
                    "old_severity": old.severity,
                    "new_severity": evidence.severity,
                    "old_review_status": old.review_status,
                    "new_review_status": evidence.review_status,
                    "summary": evidence.summary,
                }
            )
    for key, evidence in previous_map.items():
        if key in current_map:
            continue
        removed.append(
            {
                "id": evidence.id,
                "label": _evidence_identity_label(evidence),
                "severity": evidence.severity,
                "summary": evidence.summary,
                "review_status": evidence.review_status,
            }
        )

    # 上次已排除、本次重新出现：证据身份键变了（例如文件替换后文本块重切），
    # 但内容摘要高度相似且上次结论是「正常 / 排除」。这是审计最关心的一类变化。
    previous_excluded = [
        evidence
        for evidence in previous_evidences
        if evidence.review_status == ReviewStatus.EXCLUDED.value
    ]
    reappeared = []
    for item in added:
        if not previous_excluded:
            break
        for old in previous_excluded:
            if old.summary and item["summary"] and (
                old.summary[:40] == item["summary"][:40]
                or old.summary.split("存在")[0] == item["summary"].split("存在")[0]
            ):
                reappeared.append(
                    {
                        **item,
                        "previous_review_status": old.review_status,
                        "previous_summary": old.summary,
                    }
                )
                break

    def files_of(task: AnalysisTask) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for company in (task.company_snapshot or {}).get("companies", []):
            for file in company.get("files") or []:
                result[file["id"]] = {
                    "company_name": company.get("company_name"),
                    "file_name": file.get("file_name"),
                    "version": file.get("version"),
                    "file_hash": file.get("file_hash"),
                }
        return result

    files_current = files_of(current)
    files_previous = files_of(previous)
    file_added = [{"id": key, **files_current[key]} for key in files_current.keys() - files_previous.keys()]
    file_removed = [{"id": key, **files_previous[key]} for key in files_previous.keys() - files_current.keys()]
    file_changed = [
        {
            "id": key,
            "company_name": files_current[key]["company_name"],
            "file_name": files_current[key]["file_name"],
            "old_version": files_previous[key]["version"],
            "new_version": files_current[key]["version"],
        }
        for key in files_current.keys() & files_previous.keys()
        if files_current[key]["file_hash"] != files_previous[key]["file_hash"]
    ]

    def risks_of(task: AnalysisTask) -> dict[str, str]:
        result: dict[str, str] = {}
        for pair in db.scalars(
            select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id)
        ):
            result[f"{pair.company_a_id}|{pair.company_b_id}"] = pair.overall_risk
        return result

    risks_current = risks_of(current)
    risks_previous = risks_of(previous)
    risk_changes = [
        {
            "pair_key": key,
            "old_risk": risks_previous.get(key),
            "new_risk": risks_current.get(key),
        }
        for key in risks_current.keys() | risks_previous.keys()
        if risks_current.get(key) != risks_previous.get(key)
    ]

    rules_current = (current.analysis_config or {}).get("rule_versions") or {}
    rules_previous = (previous.analysis_config or {}).get("rule_versions") or {}
    rule_changes = [
        {
            "dimension": dimension,
            "old_version": (rules_previous.get(dimension) or {}).get("version_no"),
            "new_version": (rules_current.get(dimension) or {}).get("version_no"),
        }
        for dimension in rules_current.keys() | rules_previous.keys()
        if (rules_current.get(dimension) or {}).get("version_no")
        != (rules_previous.get(dimension) or {}).get("version_no")
    ]

    return {
        "current": {"id": current.id, "task_no": current.task_no},
        "previous": {"id": previous.id, "task_no": previous.task_no},
        "inputs": {
            "files_added": file_added,
            "files_removed": file_removed,
            "files_changed": file_changed,
            "rule_changes": rule_changes,
        },
        "evidences": {
            "added": added,
            "removed": removed,
            "changed": changed,
            "reappeared": reappeared,
        },
        "risks": risk_changes,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "reappeared": len(reappeared),
            "risk_changes": len(risk_changes),
        },
    }


def _pair_evidences(db: Session, pair: ComparisonPair) -> list[ComparisonEvidence]:
    """组合下的全部证据（含同时涉及两家企业的价格异常证据）。"""
    task = db.get(AnalysisTask, pair.analysis_task_id)
    evidences = [evidence for evidence in pair.evidences if evidence.is_current]
    if task:
        price_evidences = _price_evidences(db, task)
        for evidence in price_evidences:
            participant_ids = {p.company_id for p in evidence.participants}
            if {pair.company_a_id, pair.company_b_id} <= participant_ids:
                evidences.append(evidence)
    evidences.sort(key=lambda e: (_RISK_ORDER.get(e.severity, 0), e.created_at), reverse=True)
    return evidences


def pair_detail(db: Session, pair: ComparisonPair) -> dict:
    task = db.get(AnalysisTask, pair.analysis_task_id)
    names = _snapshot_names(task)
    evidences = _pair_evidences(db, pair)
    counts = Counter(e.evidence_type for e in evidences)
    text_evidences = [e for e in evidences if e.evidence_type == EvidenceType.TEXT.value]
    excluded = sum(
        1 for e in text_evidences if (e.evidence_data or {}).get("excluded_by_tender")
    )
    templated = sum(
        1 for e in text_evidences if (e.evidence_data or {}).get("excluded_by_template")
    )
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
            "forensic_risk": pair.forensic_risk,
            "overall_risk": pair.overall_risk,
            "risk_reasons": pair.risk_reasons,
            "status": pair.status,
            "error_message": pair.error_message,
        },
        "counts": {
            "text": counts[EvidenceType.TEXT.value],
            "personnel": counts[EvidenceType.PERSONNEL.value],
            "price": counts[EvidenceType.PRICE.value],
            "file": counts[EvidenceType.FILE.value],
            "metadata": counts[EvidenceType.METADATA.value],
        },
        # 统一数量口径：识别数 / 两类排除数 / 有效数。
        # 排除只针对文本证据；文件指纹与元数据证据不存在「公共内容」概念。
        "count_breakdown": {
            "text": {
                "identified": len(text_evidences),
                "excluded_by_tender": excluded,
                "excluded_by_template": templated,
                "effective": len(text_evidences) - excluded - templated,
            }
        },
        "evidences": _with_group_positions([_evidence_summary(e) for e in evidences]),
    }


def pair_evidences(
    db: Session,
    pair: ComparisonPair,
    *,
    evidence_type: str | None = None,
    review_status: str | None = None,
    severity: str | None = None,
    tender: str | None = None,
    cluster_lead_only: bool = False,
    min_cluster_size: int | None = None,
    review_queue: str | None = None,
    page: int = 1,
    page_size: int = 20,
    before: int = evidence_context.DEFAULT_BEFORE_CHARS,
    after: int = evidence_context.DEFAULT_AFTER_CHARS,
) -> dict:
    """按需返回证据及其默认上下文（比对分析页首屏只加载这一份数据）。"""
    evidences = _pair_evidences(db, pair)
    counts = Counter(e.evidence_type for e in evidences)
    filtered = []
    for evidence in evidences:
        if evidence_type and evidence.evidence_type != evidence_type:
            continue
        if review_status and evidence.review_status != review_status:
            continue
        if severity and evidence.severity != severity:
            continue
        if cluster_lead_only and evidence.cluster_lead_id not in (None, evidence.id):
            continue
        if min_cluster_size and (evidence.cluster_size or 1) < min_cluster_size:
            continue
        if review_queue == "pending" and evidence.review_status != ReviewStatus.PENDING.value:
            continue
        if review_queue == "reviewed" and evidence.review_status == ReviewStatus.PENDING.value:
            continue
        data = evidence.evidence_data or {}
        excluded_tender = bool(data.get("excluded_by_tender"))
        excluded_template = bool(data.get("excluded_by_template"))
        if tender == "hit" and not excluded_tender:
            continue
        if tender == "template" and not excluded_template:
            continue
        # 「有效」= 两类排除都不命中的证据
        if tender == "effective" and (excluded_tender or excluded_template):
            continue
        filtered.append(evidence)

    total = len(filtered)
    # 聚类字段由服务端预先算好（cluster_index/size），分页不会改变「第 N / 共 M」；
    # 排序把线索整体放在一起，合并出来的大线索更容易被先看到。
    filtered.sort(
        key=lambda item: (
            -{"high": 3, "medium": 2, "low": 1}.get(item.severity, 0),
            -(item.cluster_size or 1),
            item.cluster_id or "",
            item.cluster_index or 0,
        )
    )
    start = max(0, (page - 1) * page_size)
    rows = filtered[start : start + page_size]
    reader = evidence_context.BlockReader(db)
    items = []
    for evidence in rows:
        summary = _evidence_summary(evidence)
        summary["sides"] = evidence_context.build_sides(
            db,
            evidence,
            before=before,
            after=after,
            reader=reader,
        )
        items.append(summary)
    _with_group_positions(items)
    return {
        "pair_id": pair.id,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "counts": {
            "text": counts[EvidenceType.TEXT.value],
            "personnel": counts[EvidenceType.PERSONNEL.value],
            "price": counts[EvidenceType.PRICE.value],
            "file": counts[EvidenceType.FILE.value],
            "metadata": counts[EvidenceType.METADATA.value],
        },
        "context": {
            "before_chars": before,
            "after_chars": after,
            "step_chars": evidence_context.CONTEXT_STEP_CHARS,
            "max_chars": evidence_context.MAX_CONTEXT_CHARS,
        },
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
    data = evidence.evidence_data or {}
    return {
        "id": evidence.id,
        "evidence_type": evidence.evidence_type,
        "subtype": evidence.evidence_subtype,
        "severity": evidence.severity,
        "summary": evidence.summary,
        "review_status": evidence.review_status,
        # 是否命中招标文件公共内容（被排除的雷同内容仍保留识别记录）
        "excluded_by_tender": bool(data.get("excluded_by_tender")),
        # 是否属于批次共同模板（行业范本/通用套话，同样保留记录但不计风险）
        "excluded_by_template": bool(data.get("excluded_by_template")),
        "template_presence": data.get("template_presence"),
        "pair_id": evidence.comparison_pair_id,
        "source_a": _source_summary(evidence.source_a),
        "source_b": _source_summary(evidence.source_b),
        "companies": [p.company_name for p in evidence.participants],
        "created_at": evidence.created_at,
        # 同一组候选文本块可能命中多处相似片段，用候选标识分组展示
        "group_key": _group_key(data),
        "matched_chars": data.get("matched_chars"),
        "similarity_percent": data.get("similarity_percent"),
        "match_interval_count": data.get("match_interval_count"),
        # 线索（聚类）：同一段落的跨页/跨块命中合成一条，审核按线索进行
        "cluster_id": evidence.cluster_id,
        "cluster_size": evidence.cluster_size or 1,
        "cluster_index": evidence.cluster_index or 0,
        "cluster_lead_id": evidence.cluster_lead_id,
        "is_cluster_lead": evidence.cluster_lead_id == evidence.id,
        # 审核与分派
        "assignee_id": evidence.assignee_id,
    }


def _group_key(data: dict) -> str | None:
    """同一组候选文本块的证据共享同一个分组标识。"""
    chunk_a = data.get("chunk_a_id")
    chunk_b = data.get("chunk_b_id")
    if not chunk_a or not chunk_b:
        return None
    return f"{chunk_a}|{chunk_b}"


def _with_group_positions(items: list[dict]) -> list[dict]:
    """给同一组内的证据标注「第 N 处 / 共 M 处」。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        key = item.get("group_key")
        if key:
            groups[key].append(item)
    for members in groups.values():
        total = len(members)
        for index, member in enumerate(members, start=1):
            member["group_index"] = index
            member["group_size"] = total
    for item in items:
        if not item.get("group_key"):
            item["group_index"] = None
            item["group_size"] = None
    return items


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
        # 双方命中片段与默认上下文，供三栏对照直接渲染
        "sides": evidence_context.build_sides(db, evidence),
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
        "assignee_id": evidence.assignee_id,
        "cluster_id": evidence.cluster_id,
        "cluster_size": evidence.cluster_size or 1,
        "cluster_index": evidence.cluster_index or 0,
        "cluster_lead_id": evidence.cluster_lead_id,
        "cluster_members": _cluster_members(db, evidence),
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


def _cluster_members(db: Session, evidence: ComparisonEvidence) -> list[dict]:
    """同一线索下的全部证据（轻量摘要），供三栏页折叠展示。"""
    if not evidence.cluster_id or (evidence.cluster_size or 1) <= 1:
        return []
    rows = db.scalars(
        select(ComparisonEvidence)
        .where(
            ComparisonEvidence.cluster_id == evidence.cluster_id,
            ComparisonEvidence.is_current.is_(True),
        )
        .order_by(ComparisonEvidence.cluster_index)
    )
    return [
        {
            "id": row.id,
            "cluster_index": row.cluster_index,
            "severity": row.severity,
            "review_status": row.review_status,
            "summary": row.summary,
            "page_index": (row.source_a or {}).get("page_index"),
        }
        for row in rows
    ]


def evidence_context_payload(
    db: Session,
    evidence: ComparisonEvidence,
    side: str,
    before: int,
    after: int,
) -> dict:
    """按需读取某一侧的扩展上下文。"""
    try:
        return evidence_context.build_side_context(
            db, evidence, side, before=before, after=after
        )
    except ValueError as exc:
        raise ValueError(str(exc))


def _write_review(
    db: Session,
    evidence: ComparisonEvidence,
    new_status: str,
    comment: str | None,
    reviewer: User,
    *,
    template_fragment_id: str | None = None,
    action: str = "evidence.review",
) -> ReviewRecord:
    """写一条审核记录的**唯一入口**（单条 / 批量 / 按线索都走这里）。

    审核是单级的：一次审核即结论，没有二审。``review_status`` 就是当前结论，
    ``pending`` 表示撤回审核、回到待审核队列。
    """
    record = ReviewRecord(
        evidence_id=evidence.id,
        reviewer_id=reviewer.id,
        reviewer_name=reviewer.display_name or reviewer.employee_no or reviewer.username,
        old_status=evidence.review_status,
        new_status=new_status,
        comment=comment,
        template_fragment_id=template_fragment_id,
    )
    db.add(record)
    evidence.review_status = new_status

    audit.log_audit(
        db,
        action,
        "comparison_evidence",
        evidence.id,
        reviewer,
        {"old_status": record.old_status, "new_status": new_status},
    )
    return record


def apply_review(
    db: Session,
    evidence: ComparisonEvidence,
    new_status: str,
    comment: str | None,
    reviewer: User,
    *,
    template_fragment_id: str | None = None,
) -> ComparisonEvidence:
    _write_review(
        db,
        evidence,
        new_status,
        comment,
        reviewer,
        template_fragment_id=template_fragment_id,
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

    # 全系统统一的证据编号：报告里的编号必须能直接在风险页查到同一条证据
    from app.services import risk_service

    codes = risk_service.evidence_codes(db, evidences)

    def reviewer_summary(evidence: ComparisonEvidence) -> str:
        records = sorted(
            evidence.review_records, key=lambda r: r.created_at or evidence.created_at
        )
        if not records:
            return ""
        last = records[-1]
        return f"{last.reviewer_name}（{REVIEW_LABELS.get(last.new_status, last.new_status)}）"

    def reviewed_at(evidence: ComparisonEvidence) -> str:
        if not evidence.review_records:
            return ""
        last = max(evidence.review_records, key=lambda r: r.created_at or evidence.created_at)
        return _format_display_time(last.created_at)

    def review_comment(evidence: ComparisonEvidence) -> str:
        return "; ".join(
            f"{r.reviewer_name}：{r.comment}" for r in evidence.review_records if r.comment
        )

    def source_location(source: dict | None) -> str:
        """把来源渲染成可读的「文件 · 第 N 页」，缺失时明确写「未记录」。"""
        if not source:
            return "未记录"
        file_name = source.get("file_name") or ""
        page = source.get("printed_page_number") or source.get("page_index")
        if file_name and page not in (None, ""):
            return f"{file_name} 第{page}页"
        if file_name:
            return file_name
        if page not in (None, ""):
            return f"第{page}页"
        return "未记录"

    def evidence_source_text(evidence: ComparisonEvidence) -> str:
        if evidence.evidence_type == EvidenceType.PRICE.value:
            data = evidence.evidence_data or {}
            parts = [
                source_location(quote.get("source"))
                for quote in (data.get("quotes") or [])
                if quote.get("source")
            ]
            if parts:
                return "；".join(dict.fromkeys(parts))
            return "人工录入 / 未记录页码"
        return (
            f"A：{source_location(evidence.source_a)}；"
            f"B：{source_location(evidence.source_b)}"
        )

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
        # 与概览接口同口径：项目级价格规则 + 组合级逐项单价雷同
        price_count = (
            sum(
                1
                for evidence in price_evidences
                if {pair.company_a_id, pair.company_b_id}
                <= {p.company_id for p in evidence.participants}
            )
            + counts[pair.id][EvidenceType.PRICE.value]
        )
        overview_rows.append(
            [
                index,
                names.get(pair.company_a_id, ""),
                names.get(pair.company_b_id, ""),
                counts[pair.id][EvidenceType.TEXT.value],
                counts[pair.id][EvidenceType.PERSONNEL.value],
                price_count,
                counts[pair.id][EvidenceType.FILE.value],
                counts[pair.id][EvidenceType.METADATA.value],
                RISK_LABELS.get(pair.text_risk, pair.text_risk),
                RISK_LABELS.get(pair.personnel_risk, pair.personnel_risk),
                RISK_LABELS.get(pair.price_risk, pair.price_risk),
                RISK_LABELS.get(pair.forensic_risk, pair.forensic_risk),
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
            "文件指纹数",
            "元数据证据数",
            "文本风险",
            "人员风险",
            "价格风险",
            "取证风险",
            "综合风险",
            "状态",
        ],
        overview_rows,
        [8, 26, 26, 12, 12, 16, 12, 14, 10, 10, 10, 10, 12, 10],
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
                codes.get(evidence.id, ""),
                data.get("matched_text", ""),
                source_a.get("company_name", ""),
                source_a.get("file_name", ""),
                source_a.get("printed_page_number") or source_a.get("page_index", ""),
                source_b.get("company_name", ""),
                source_b.get("file_name", ""),
                source_b.get("printed_page_number") or source_b.get("page_index", ""),
                data.get("matched_chars", 0),
                data.get("similarity_percent", 0),
                "是" if data.get("excluded_by_tender") else "否",
                "是" if data.get("excluded_by_template") else "否",
                REVIEW_LABELS.get(evidence.review_status, evidence.review_status),
                reviewer_summary(evidence),
                reviewed_at(evidence),
                review_comment(evidence),
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
            "共同模板",
            "审核状态",
            "审核人",
            "审核时间",
            "审核说明",
        ],
        text_rows,
        [8, 26, 60, 22, 20, 10, 22, 20, 10, 10, 12, 14, 12, 16, 16, 18, 40],
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
                codes.get(evidence.id, ""),
                name,
                companies,
                roles,
                files,
                pages,
                len(occurrences),
                data.get("identity_info", ""),
                REVIEW_LABELS.get(evidence.review_status, evidence.review_status),
                reviewer_summary(evidence),
                reviewed_at(evidence),
                review_comment(evidence),
                data.get("note", ""),
            ]
        )
    write_sheet(
        ws,
        [
            "序号",
            "证据编号",
            "重复姓名",
            "涉及企业",
            "对应岗位",
            "对应文件",
            "对应页码",
            "出现次数",
            "身份辅助信息",
            "审核状态",
            "审核人",
            "审核时间",
            "审核说明",
            "备注",
        ],
        person_rows,
        [8, 26, 14, 30, 20, 30, 34, 10, 20, 16, 16, 18, 40, 40],
    )

    # 3.5 报价汇总表：不论是否触发价格异常，都列出全部参与企业的报价与来源。
    # 只有「价格异常分析表」时，没有异常就只剩表头，自动识别的金额、页码
    # 与覆盖情况都无从复核（QA：覆盖 4/4 却静默报错价，无法被发现）。
    quote_entries = ((task.analysis_config or {}).get("price_snapshot") or {}).get("entries") or {}
    # 当前有效的人工报价：用于判断自动识别的「待确认」是否已经被人工作答，
    # 避免页面已改人工报价、导出仍标「待确认」的前后不一致。
    manual_quotes = {
        quote.company_id: quote
        for quote in db.scalars(
            select(PriceQuote).where(
                PriceQuote.project_id == project.id,
                PriceQuote.is_active.is_(True),
            )
        )
    }
    quote_rows = []
    for company in (task.company_snapshot or {}).get("companies", []):
        company_id = company.get("company_id")
        entry = quote_entries.get(company_id) or {}
        amount = entry.get("amount")
        source = entry.get("source") or {}
        origin = entry.get("source_kind") or "auto"
        manual = manual_quotes.get(company_id)
        # 人工报价等于任务快照里的金额时，待确认项视为已确认
        confirmed_manually = bool(
            manual is not None
            and amount is not None
            and abs(float(manual.amount) - float(amount)) <= 0.01
        )
        quote_rows.append(
            [
                len(quote_rows) + 1,
                company.get("company_name", ""),
                round(float(amount), 2) if amount is not None else "",
                entry.get("amount_upper") or "",
                QUOTE_SOURCE_LABELS.get(origin, origin),
                source_location(source) if source.get("file_name") else ("人工录入" if origin in {"manual", "import"} else "未记录"),
                entry.get("raw") or "",
                "是" if entry.get("needs_confirmation") and not confirmed_manually else "否",
                "、".join(
                    f"{float(value):,.2f}" for value in (entry.get("alternative_amounts") or [])
                ),
            ]
        )
    write_sheet(
        wb.create_sheet("报价汇总表"),
        [
            "序号",
            "投标人名称",
            "投标总报价（元）",
            "大写金额",
            "来源",
            "原文位置",
            "识别原文",
            "是否待确认",
            "候选金额",
        ],
        quote_rows,
        [8, 28, 18, 30, 12, 28, 44, 12, 30],
    )

    # 4. 价格异常分析表
    ws = wb.create_sheet("价格异常分析表")
    price_rows = []
    line_item_rows = []
    for evidence in price_evidences:
        data = evidence.evidence_data
        rule_label = data.get("rule_label", evidence.evidence_subtype)
        # 逐项单价雷同的证据结构是 items，不是 quotes，必须单独展开，
        # 否则这类证据在价格表里整行消失。
        if evidence.evidence_subtype == "line_item_duplicate":
            for item in data.get("items", []):
                line_item_rows.append(
                    [
                        len(line_item_rows) + 1,
                        codes.get(evidence.id, ""),
                        item.get("name", ""),
                        item.get("unit_price_a", ""),
                        item.get("unit_price_b", ""),
                        round((item.get("relative_diff") or 0) * 100, 3),
                        "是" if item.get("same_price") else "否",
                        data.get("shared_count", ""),
                        data.get("identical_count", ""),
                        rule_label,
                        RISK_LABELS.get(evidence.severity, evidence.severity),
                        data.get("note", ""),
                        REVIEW_LABELS.get(evidence.review_status, evidence.review_status),
                        reviewer_summary(evidence),
                        reviewed_at(evidence),
                        review_comment(evidence),
                    ]
                )
            continue
        for quote in data.get("quotes", []):
            source = quote.get("source") or {}
            price_rows.append(
                [
                    len(price_rows) + 1,
                    codes.get(evidence.id, ""),
                    quote.get("company_name", ""),
                    quote.get("amount", ""),
                    quote.get("amount_upper", ""),
                    round((quote.get("deviation_rate") or 0) * 100, 3),
                    "是" if "sequence" in evidence.evidence_subtype else "否",
                    str(int(float(quote.get("amount") or 0)))[-3:].zfill(3),
                    rule_label,
                    RISK_LABELS.get(evidence.severity, evidence.severity),
                    # 人工录入的报价没有原文页码，明确写出来而不是留「第页」
                    source_location(source)
                    if source.get("file_name")
                    else ("人工录入" if quote.get("origin") in {"manual", "import"} else "未记录"),
                    data.get("note", ""),
                    REVIEW_LABELS.get(evidence.review_status, evidence.review_status),
                    reviewer_summary(evidence),
                    reviewed_at(evidence),
                    review_comment(evidence),
                ]
            )
    write_sheet(
        ws,
        [
            "序号",
            "证据编号",
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
            "审核状态",
            "审核人",
            "审核时间",
            "审核说明",
        ],
        price_rows,
        [8, 26, 28, 18, 30, 12, 14, 12, 26, 10, 28, 40, 16, 16, 18, 40],
    )

    if line_item_rows:
        ws = wb.create_sheet("逐项单价对比表")
        write_sheet(
            ws,
            [
                "序号",
                "证据编号",
                "分项名称",
                "企业A单价",
                "企业B单价",
                "单价差异(%)",
                "价格相同",
                "共同分项数",
                "完全相同条数",
                "异常类型",
                "风险提示",
                "备注",
                "审核状态",
                "审核人",
                "审核时间",
                "审核说明",
            ],
            line_item_rows,
            [8, 26, 36, 16, 16, 14, 12, 14, 14, 26, 10, 40, 16, 16, 18, 40],
        )

    # 5. 文件取证表：文件指纹与文档元数据。它和前面三张内容表正交，
    #    改抬头、换措辞都洗不掉，是评审材料里最直接的一类证据。
    ws = wb.create_sheet("文件取证表")
    forensics_rows = []
    for evidence in evidences:
        if evidence.evidence_type not in {
            EvidenceType.FILE.value,
            EvidenceType.METADATA.value,
        }:
            continue
        data = evidence.evidence_data or {}
        source_a = evidence.source_a or {}
        source_b = evidence.source_b or {}
        detail = _forensics_detail(evidence, data)
        forensics_rows.append(
            [
                len(forensics_rows) + 1,
                codes.get(evidence.id, ""),
                SUBTYPE_LABELS.get(evidence.evidence_subtype)
                or TYPE_LABELS.get(evidence.evidence_type, evidence.evidence_type),
                source_a.get("company_name", ""),
                source_a.get("file_name", ""),
                source_b.get("company_name", ""),
                source_b.get("file_name", ""),
                detail,
                RISK_LABELS.get(evidence.severity, evidence.severity),
                data.get("note", ""),
                REVIEW_LABELS.get(evidence.review_status, evidence.review_status),
                reviewer_summary(evidence),
                reviewed_at(evidence),
                review_comment(evidence),
            ]
        )
    write_sheet(
        ws,
        [
            "序号",
            "证据编号",
            "证据类型",
            "企业A",
            "文件A",
            "企业B",
            "文件B",
            "取证内容",
            "风险提示",
            "备注",
            "审核状态",
            "审核人",
            "审核时间",
            "审核说明",
        ],
        forensics_rows,
        [8, 26, 14, 28, 34, 28, 34, 46, 10, 44, 16, 16, 18, 40],
    )

    # 6. 审核记录表：只追加的审核历史，报告可独立还原审核过程
    review_rows = []
    for evidence in evidences:
        records = sorted(
            evidence.review_records, key=lambda r: r.created_at or evidence.created_at
        )
        for record in records:
            review_rows.append(
                [
                    len(review_rows) + 1,
                    codes.get(evidence.id, ""),
                    TYPE_LABELS.get(evidence.evidence_type, evidence.evidence_type),
                    evidence.summary,
                    record.reviewer_name,
                    REVIEW_LABELS.get(record.old_status, record.old_status),
                    REVIEW_LABELS.get(record.new_status, record.new_status),
                    record.comment or "",
                    _format_display_time(record.created_at)
                    if record.created_at
                    else "",
                ]
            )
    if review_rows:
        ws = wb.create_sheet("审核记录表")
        write_sheet(
            ws,
            [
                "序号",
                "证据编号",
                "证据类型",
                "证据摘要",
                "审核人",
                "原结论",
                "新结论",
                "审核说明",
                "审核时间",
            ],
            review_rows,
            [8, 26, 14, 60, 16, 16, 16, 50, 20],
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
