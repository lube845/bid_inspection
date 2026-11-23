from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_company, get_current_user, get_project
from app.api.serializers import file_payload
from app.db.session import get_db
from app.models import OriginalFile, User
from app.models.enums import FileKind
from app.schemas.company import CompanyCreate, CompanyUpdate, FileOrderUpdate
from app.services import (
    access_service,
    company_service,
    file_service,
    logical_document_service,
    project_service,
)

router = APIRouter(tags=["companies"])


def _files_by_company(db: Session, project_id: str) -> dict[str, list[OriginalFile]]:
    files = list(
        db.scalars(
            select(OriginalFile)
            .where(
                OriginalFile.project_id == project_id,
                OriginalFile.kind == FileKind.BID.value,
                OriginalFile.is_active.is_(True),
            )
            .order_by(OriginalFile.sort_order, OriginalFile.uploaded_at)
        )
    )
    grouped: dict[str, list[OriginalFile]] = {}
    for file in files:
        grouped.setdefault(file.company_id, []).append(file)
    return grouped


@router.get("/projects/{project_id}/companies")
def list_companies(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = get_project(db, project_id, user)
    files = _files_by_company(db, project.id)
    companies = project_service.active_companies(db, project.id)
    return {
        "items": [
            project_service.company_summary(company, files.get(company.id, []))
            for company in companies
        ],
        "total": len(companies),
        "limit": 20,
    }


@router.post("/projects/{project_id}/companies", status_code=201)
def create_company(
    project_id: str,
    payload: CompanyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = get_project(db, project_id, user, access_service.MANAGE)
    company = company_service.create_company(
        db,
        project,
        payload.company_name,
        payload.credit_code,
        payload.remark,
        user,
        credit_code_type=payload.credit_code_type,
    )
    return project_service.company_summary(company, [])


@router.get("/companies/{company_id}")
def get_company_detail(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = get_company(db, company_id, user)
    files = logical_document_service.active_company_files(db, company.id)
    payload = project_service.company_summary(company, files)
    payload["files"] = [file_payload(f, company) for f in files]
    payload["logical_document"] = logical_document_service.logical_document_preview(
        db, company, include_pages=False
    )
    # 该企业的价格资料：总价（人工/导入）+ 分项单价来源与条数
    from app.services import quote_service

    quote = quote_service._active_quote(db, company.project_id, company.id)
    items = quote_service.list_price_items(db, get_project(db, company.project_id, user)).get(
        company.id, []
    )
    payload["price"] = {
        "total_amount": float(quote.amount) if quote else None,
        "total_source": quote.source_kind if quote else None,
        "total_updated_at": quote.updated_at if quote else None,
        "item_count": len(items),
        "item_source": items[0]["source_kind"] if items else None,
        "item_file": (items[0].get("source_ref") or {}).get("filename") if items else None,
    }
    return payload



@router.patch("/companies/{company_id}")
def update_company(
    company_id: str,
    payload: CompanyUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = get_company(db, company_id, user, access_service.MANAGE)
    company = company_service.update_company(db, company, payload, user)
    files = logical_document_service.active_company_files(db, company.id)
    return project_service.company_summary(company, files)


@router.delete("/companies/{company_id}")
def delete_company(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = get_company(db, company_id, user, access_service.MANAGE)
    company_service.delete_company(db, company, user)
    return {"message": "企业已删除"}


@router.post("/companies/{company_id}/confirm")
def confirm_company(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    # 确认企业资料会改变企业状态，属于管理动作
    company = get_company(db, company_id, user, access_service.MANAGE)
    company = file_service.confirm_company(db, company, user)
    files = logical_document_service.active_company_files(db, company.id)
    return project_service.company_summary(company, files)


@router.put("/companies/{company_id}/file-order")
def update_file_order(
    company_id: str,
    payload: FileOrderUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = get_company(db, company_id, user, access_service.MANAGE)
    file_service.reorder_files(db, company, payload.file_ids)
    files = logical_document_service.active_company_files(db, company.id)
    result = project_service.company_summary(company, files)
    result["files"] = [file_payload(f, company) for f in files]
    result["logical_document"] = logical_document_service.logical_document_preview(
        db, company, include_pages=False
    )
    return result


@router.get("/companies/{company_id}/logical-document")
def logical_document(
    company_id: str,
    include_pages: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = get_company(db, company_id, user)
    return logical_document_service.logical_document_preview(db, company, include_pages)
