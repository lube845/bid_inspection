from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, parse_date_bound
from app.db.session import get_db
from app.models import ComparisonEvidence, User
from app.schemas.risk import BatchAssignRequest, BatchReviewRequest, ClusterReviewRequest
from app.services import access_service, audit, risk_service

router = APIRouter(tags=["risks"])


@router.get("/risks/filters")
def risk_filters(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """筛选下拉的可选项（项目、企业、审核人员、可分派用户）。"""
    payload = risk_service.risk_filters(db, user)
    payload["assignees"] = _assignable_users(db)
    return payload


def _ensure_project_access(db: Session, project_id: str, user: User, role: str) -> None:
    try:
        access_service.ensure_access_by_id(db, project_id, user, role)
    except access_service.ProjectAccessError as exc:
        raise HTTPException(403 if exc.forbidden else 404, str(exc)) from exc


def _ensure_evidence_access(
    db: Session, evidence_id: str, user: User, role: str = access_service.VIEW
) -> ComparisonEvidence:
    evidence = db.get(ComparisonEvidence, evidence_id)
    if not evidence:
        raise HTTPException(404, "证据不存在")
    _ensure_project_access(db, evidence.project_id, user, role)
    return evidence


def _guard_batch(db: Session, evidence_ids: list[str], user: User, role: str) -> None:
    """批量操作逐条校验所属项目权限。

    两条规则都必要：
    - 不存在的证据直接 404——否则传一批伪造 id 就能绕过校验（服务层会静默跳过）；
    - 只要有一条越权就整体拒绝，不做「部分成功」，避免用户以为全都生效。
    """
    wanted = list(dict.fromkeys(evidence_ids))
    found = dict(
        db.execute(
            select(ComparisonEvidence.id, ComparisonEvidence.project_id).where(
                ComparisonEvidence.id.in_(wanted)
            )
        ).all()
    ) if wanted else {}
    missing = [item for item in wanted if item not in found]
    if missing:
        raise HTTPException(404, f"证据不存在：{missing[0]}")
    for project_id in set(found.values()):
        _ensure_project_access(db, project_id, user, role)


def _assignable_users(db: Session) -> list[dict]:
    """可分派用户：全部启用的账号。

    普通用户拿不到 /system/users（管理员接口），但分派下拉需要这份名单，
    因此这里只返回最小字段集，不暴露账号、密码状态等敏感信息。
    """
    rows = db.scalars(
        select(User).where(User.is_active.is_(True)).order_by(User.display_name)
    )
    return [
        {
            "id": row.id,
            "display_name": row.display_name or row.employee_no or row.username,
            "role": row.role,
        }
        for row in rows
    ]


@router.get("/risks")
def list_risks(
    project_id: str | None = None,
    company_id: str | None = None,
    evidence_type: str | None = None,
    severity: str | None = None,
    review_status: str | None = None,
    reviewer_id: str | None = None,
    assignee_id: str | None = None,
    review_queue: str | None = Query(default=None, pattern="^(pending|reviewed)$"),
    cluster_lead_only: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    include_history: bool = Query(
        default=False, description="为 true 时同时返回历史任务的证据（默认只看最新任务）"
    ),
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
        assignee_id=assignee_id,
        review_queue=review_queue,
        cluster_lead_only=cluster_lead_only,
        date_from=parse_date_bound(date_from, end=False),
        date_to=parse_date_bound(date_to, end=True),
        include_history=include_history,
        page=page,
        page_size=page_size,
        user_id=None if access_service.is_system_admin(user) else user.id,
    )


@router.post("/risks/batch-review")
def batch_review(
    payload: BatchReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """批量标记审核状态（V1.0 只允许标记为待进一步核查）。"""
    _guard_batch(db, payload.evidence_ids, user, access_service.REVIEW)
    try:
        result = risk_service.batch_review(
            db,
            payload.evidence_ids,
            payload.status,
            payload.comment,
            user,
            skip_reviewed=payload.skip_reviewed,
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


@router.post("/risks/batch-assign")
def batch_assign(
    payload: BatchAssignRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """批量分派证据给审核人；assignee_id 为空表示取消分派。"""
    _guard_batch(db, payload.evidence_ids, user, access_service.REVIEW)
    try:
        result = risk_service.batch_assign(db, payload.evidence_ids, payload.assignee_id, user)
    except risk_service.RiskValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.post("/risks/cluster-review")
def cluster_review(
    payload: ClusterReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """按线索审核：一次操作覆盖整条线索的证据。"""
    _guard_batch(db, payload.evidence_ids, user, access_service.REVIEW)
    try:
        result = risk_service.cluster_review(
            db,
            payload.evidence_ids,
            payload.status,
            payload.comment,
            user,
            skip_reviewed=payload.skip_reviewed,
        )
    except risk_service.RiskValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/risks/{evidence_id}")
def risk_detail(
    evidence_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """风险详情：证据信息、原始页码、涉及企业、历史审核记录。"""
    evidence = _ensure_evidence_access(db, evidence_id, user)
    return risk_service.risk_detail(db, evidence)

