from datetime import datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import ComparisonEvidence, User
from app.schemas.risk import BatchReviewRequest
from app.services import audit, risk_service

router = APIRouter(tags=["risks"])


@router.get("/risks/filters")
def risk_filters(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """筛选下拉的可选项（项目、企业、审核人员）。"""
    return risk_service.risk_filters(db)


@router.get("/risks")
def list_risks(
    project_id: str | None = None,
    company_id: str | None = None,
    evidence_type: str | None = None,
    severity: str | None = None,
    review_status: str | None = None,
    reviewer_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """跨项目的异常证据清单。"""
    return risk_service.list_risks(
        db,
        project_id=project_id,
        company_id=company_id,
        evidence_type=evidence_type,
        severity=severity,
        review_status=review_status,
        reviewer_id=reviewer_id,
        date_from=_parse_date(date_from, end=False),
        date_to=_parse_date(date_to, end=True),
        page=page,
        page_size=page_size,
    )


@router.post("/risks/batch-review")
def batch_review(
    payload: BatchReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """批量标记审核状态（V1.0 只允许标记为待进一步核查）。"""
    try:
        result = risk_service.batch_review(
            db, payload.evidence_ids, payload.status, payload.comment, user
        )
    except risk_service.RiskValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.log_audit(
        db,
        "evidence.review",
        "comparison_evidence",
        None,
        user,
        {
            "batch": True,
            "count": result["updated"],
            "old_status": None,
            "new_status": result["status"],
        },
    )
    db.commit()
    return result


@router.get("/risks/{evidence_id}")
def risk_detail(
    evidence_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """风险详情：证据信息、原始页码、涉及企业、历史审核记录。"""
    evidence = db.get(ComparisonEvidence, evidence_id)
    if not evidence:
        raise HTTPException(404, "证据不存在")
    return risk_service.risk_detail(db, evidence)


def _parse_date(value: str | None, end: bool = False):
    """把 YYYY-MM-DD 解析成 UTC 边界，避免 SQLite/Postgres 时区不一致。"""
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if end:
        parsed = datetime.combine(parsed.date(), time.max)
    return parsed.replace(tzinfo=timezone.utc)
