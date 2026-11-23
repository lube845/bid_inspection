from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import error_response, get_company, get_current_user, get_project
from app.db.session import get_db
from app.models import Company, User
from app.schemas.quote import QuoteEntry
from app.services import (
    access_service,
    company_price_service,
    price_matrix_service,
    quote_service,
)
from app.services.quote_service import QuoteError

router = APIRouter(tags=["quotes"])

_MAX_IMPORT_BYTES = 10 * 1024 * 1024


@router.get("/projects/{project_id}/quotes")
def list_quotes(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """项目内各企业的当前有效报价（含未录入的企业）。"""
    project = get_project(db, project_id, user)
    return {"items": quote_service.list_quotes(db, project)}


@router.get("/projects/{project_id}/price-matrix")
def price_matrix(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """项目内报价的可视化比对：总价一览 + 「企业 × 清单项」单价矩阵。

    单价来自最新分析任务的正文解析结果，与逐项单价证据同源；总价沿用报价
    清单页的口径（人工确认 > 自动识别）。
    """
    project = get_project(db, project_id, user)
    return price_matrix_service.build_price_matrix(db, project)


@router.get("/projects/{project_id}/quotes/template")
def download_template(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    get_project(db, project_id, user)
    data = quote_service.build_template()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="quote-template.xlsx"'},
    )


@router.post("/projects/{project_id}/quotes/import")
async def import_quotes(
    project_id: str,
    file: UploadFile = File(...),
    dry_run: bool = Query(default=True, description="true 只预览校验结果，false 落库"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = get_project(db, project_id, user, access_service.MANAGE)
    data = await file.read()
    if len(data) > _MAX_IMPORT_BYTES:
        raise HTTPException(400, "报价单文件不能超过 10MB")
    try:
        return quote_service.import_quotes(
            db, project, file.filename or "quotes.xlsx", data, user, dry_run=dry_run
        )
    except QuoteError as exc:
        raise error_response(exc)


@router.post("/companies/{company_id}/price-file")
async def upload_company_price_file(
    company_id: str,
    file: UploadFile = File(...),
    dry_run: bool = Query(default=True, description="true 只预览解析结果，false 落库"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """上传单家企业的价格 Excel（总价 + 分项单价）。

    这是该企业的**权威报价资料**：导入后总价与分项单价都以它为最高优先级，
    覆盖正文自动识别与手工补录。
    """
    company = get_company(db, company_id, user, access_service.MANAGE)
    project = get_project(db, company.project_id, user, access_service.MANAGE)
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    if len(data) > _MAX_IMPORT_BYTES:
        raise HTTPException(400, "价格文件不能超过 10MB")
    try:
        parsed = company_price_service.parse_company_price_file(
            file.filename or "price.xlsx", data
        )
    except QuoteError as exc:
        raise error_response(exc)

    if dry_run or parsed["errors"]:
        return {
            "dry_run": True,
            "company_id": company.id,
            "company_name": company.company_name,
            "filename": file.filename,
            **parsed,
        }

    source_ref = {"filename": file.filename or "price.xlsx", "source_kind": "import"}
    if parsed["total_amount"]:
        quote_service.set_quote(
            db,
            project,
            company,
            parsed["total_amount"],
            amount_upper=parsed.get("amount_upper"),
            source_kind="import",
            source_ref=source_ref,
            raw_text="价格文件导入",
            actor=user,
            commit=False,
        )
    if parsed["items"]:
        quote_service.set_price_items(
            db,
            project,
            company,
            parsed["items"],
            source_kind="import",
            source_ref=source_ref,
            actor=user,
            commit=False,
        )
    db.commit()
    return {
        "dry_run": False,
        "company_id": company.id,
        "company_name": company.company_name,
        "filename": file.filename,
        **parsed,
    }


@router.put("/companies/{company_id}/quote")
def set_quote(
    company_id: str,
    payload: QuoteEntry,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = get_company(db, company_id, user, access_service.MANAGE)
    project = get_project(db, company.project_id, user, access_service.MANAGE)
    try:
        quote = quote_service.set_quote(
            db,
            project,
            company,
            payload.amount,
            amount_upper=payload.amount_upper,
            raw_text=payload.remark or "",
            actor=user,
        )
    except QuoteError as exc:
        raise error_response(exc)
    return {"quote": quote_service.quote_payload(quote)}


@router.delete("/companies/{company_id}/quote")
def delete_quote(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company: Company = get_company(db, company_id, user, access_service.MANAGE)
    project = get_project(db, company.project_id, user, access_service.MANAGE)
    try:
        quote_service.delete_quote(db, project, company, user)
    except QuoteError as exc:
        raise error_response(exc)
    return {"ok": True}
