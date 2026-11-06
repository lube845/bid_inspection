from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.serializers import file_payload
from app.db.session import get_db
from app.models import Company, OriginalFile, Project, User
from app.models.enums import FileKind
from app.schemas.company import CompanyCreate, CompanyUpdate, FileOrderUpdate
from app.services import company_service, file_service, logical_document_service, project_service

router = APIRouter(tags=["companies"])


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project or project.deleted_at:
        raise HTTPException(404, "项目不存在")
    return project


def _get_company(db: Session, company_id: str) -> Company:
    company = db.get(Company, company_id)
    if not company or company.deleted_at:
        raise HTTPException(404, "企业不存在")
    return company


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
    project = _get_project(db, project_id)
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
    project = _get_project(db, project_id)
    company = company_service.create_company(
        db, project, payload.company_name, payload.credit_code, payload.remark, user
    )
    return project_service.company_summary(company, [])


@router.get("/companies/{company_id}")
def get_company(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = _get_company(db, company_id)
    files = logical_document_service.active_company_files(db, company.id)
    payload = project_service.company_summary(company, files)
    payload["files"] = [file_payload(f, company) for f in files]
    payload["logical_document"] = logical_document_service.logical_document_preview(
        db, company, include_pages=False
    )
    return payload


@router.patch("/companies/{company_id}")
def update_company(
    company_id: str,
    payload: CompanyUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = _get_company(db, company_id)
    company = company_service.update_company(db, company, payload, user)
    files = logical_document_service.active_company_files(db, company.id)
    return project_service.company_summary(company, files)


@router.delete("/companies/{company_id}")
def delete_company(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = _get_company(db, company_id)
    company_service.delete_company(db, company, user)
    return {"message": "企业已删除"}


@router.post("/companies/{company_id}/confirm")
def confirm_company(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = _get_company(db, company_id)
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
    company = _get_company(db, company_id)
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
    company = _get_company(db, company_id)
    return logical_document_service.logical_document_preview(db, company, include_pages)
