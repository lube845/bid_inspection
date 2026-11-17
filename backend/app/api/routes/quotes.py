from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.routes.files import _get_company, _get_project
from app.db.session import get_db
from app.models import Company, User
from app.schemas.quote import QuoteEntry
from app.services import quote_service
from app.services.quote_service import QuoteError

router = APIRouter(tags=["quotes"])

_MAX_IMPORT_BYTES = 10 * 1024 * 1024


def _handle(exc: QuoteError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/projects/{project_id}/quotes")
def list_quotes(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """项目内各企业的当前有效报价（含未录入的企业）。"""
    project = _get_project(db, project_id)
    return {"items": quote_service.list_quotes(db, project)}


@router.get("/projects/{project_id}/quotes/template")
def download_template(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    _get_project(db, project_id)
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
    project = _get_project(db, project_id)
    data = await file.read()
    if len(data) > _MAX_IMPORT_BYTES:
        raise HTTPException(400, "报价单文件不能超过 10MB")
    try:
        return quote_service.import_quotes(
            db, project, file.filename or "quotes.xlsx", data, user, dry_run=dry_run
        )
    except QuoteError as exc:
        raise _handle(exc)


@router.put("/companies/{company_id}/quote")
def set_quote(
    company_id: str,
    payload: QuoteEntry,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = _get_company(db, company_id)
    project = _get_project(db, company.project_id)
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
        raise _handle(exc)
    return {"quote": quote_service.quote_payload(quote)}


@router.delete("/companies/{company_id}/quote")
def delete_quote(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company: Company = _get_company(db, company_id)
    project = _get_project(db, company.project_id)
    try:
        quote_service.delete_quote(db, project, company, user)
    except QuoteError as exc:
        raise _handle(exc)
    return {"ok": True}
