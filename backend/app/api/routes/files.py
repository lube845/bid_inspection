from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_user_allow_query
from app.api.serializers import file_payload, page_payload
from app.core.config import settings
from app.db.session import get_db
from app.models import Company, DocumentPage, OriginalFile, Project, User
from app.models.enums import FileKind, FileParseStatus
from app.services import audit, file_service
from app.storage.local import get_storage
from app.workers import dispatch

router = APIRouter(tags=["files"])

_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}


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


def _get_file(db: Session, file_id: str) -> OriginalFile:
    file = db.get(OriginalFile, file_id)
    if not file:
        raise HTTPException(404, "文件不存在")
    return file


@router.post("/projects/{project_id}/tender-files", status_code=201)
async def upload_tender_files(
    project_id: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    created = []
    for upload in files:
        record = await file_service.create_file(
            db, project, None, FileKind.TENDER.value, upload, actor=user
        )
        created.append(file_payload(record))
    return {"items": created, "message": f"已上传 {len(created)} 份招标文件"}


@router.post("/companies/{company_id}/files", status_code=201)
async def upload_company_files(
    company_id: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    company = _get_company(db, company_id)
    project = _get_project(db, company.project_id)
    created = []
    for upload in files:
        record = await file_service.create_file(
            db, project, company, FileKind.BID.value, upload, actor=user
        )
        created.append(file_payload(record, company))
    return {"items": created, "message": f"已上传 {len(created)} 份企业文件"}


@router.get("/projects/{project_id}/files")
def list_files(
    project_id: str,
    kind: str | None = None,
    company_id: str | None = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    query = select(OriginalFile).where(OriginalFile.project_id == project.id)
    if kind:
        query = query.where(OriginalFile.kind == kind)
    if company_id:
        query = query.where(OriginalFile.company_id == company_id)
    if not include_inactive:
        query = query.where(OriginalFile.is_active.is_(True))
    files = list(db.scalars(query.order_by(OriginalFile.uploaded_at.desc())))
    companies = {
        c.id: c
        for c in db.scalars(select(Company).where(Company.project_id == project.id))
    }
    return {
        "items": [
            file_payload(f, companies.get(f.company_id) if f.company_id else None)
            for f in files
        ],
        "total": len(files),
    }


@router.get("/files/{file_id}")
def get_file(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    file = _get_file(db, file_id)
    company = db.get(Company, file.company_id) if file.company_id else None
    payload = file_payload(file, company)
    payload["pages"] = [
        {
            "page_index": page.page_index,
            "parse_status": page.parse_status,
            "extraction_method": page.extraction_method,
            "printed_page_number": page.printed_page_number,
            "note": page.note,
            "char_count": len(page.page_text or ""),
        }
        for page in sorted(file.pages, key=lambda p: p.page_index)
    ]
    return payload


@router.delete("/files/{file_id}")
def delete_file(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    file = _get_file(db, file_id)
    if not file.is_active:
        raise HTTPException(400, "文件已失效")
    file_service.delete_file(db, file, user)
    return {"message": "文件已删除"}


@router.post("/files/{file_id}/replace")
async def replace_file(
    file_id: str,
    upload: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    file = _get_file(db, file_id)
    if not file.is_active:
        raise HTTPException(400, "只能替换当前有效文件")
    new_file = await file_service.replace_file(db, file, upload, user)
    company = db.get(Company, new_file.company_id) if new_file.company_id else None
    return file_payload(new_file, company)


@router.post("/files/{file_id}/parse")
def parse_file(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    file = _get_file(db, file_id)
    if not file.is_active:
        raise HTTPException(400, "文件已失效，不能解析")
    if file.parse_status == FileParseStatus.PARSING.value:
        return {"message": "文件正在解析中", "parse_status": file.parse_status}
    dispatch.enqueue_parse(file.id)
    db.refresh(file)
    return {
        "message": "已提交解析任务" if not settings.sync_tasks else "解析完成",
        "parse_status": file.parse_status,
        "parse_message": file.parse_message,
    }


@router.post("/projects/{project_id}/parse")
def parse_project_files(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    file_ids = file_service.trigger_parse_for_project(db, project.id)
    for file_id in file_ids:
        dispatch.enqueue_parse(file_id)
    return {"message": f"已提交 {len(file_ids)} 个文件的解析任务", "file_ids": file_ids}


@router.get("/projects/{project_id}/parse-center")
def parse_center(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    project = _get_project(db, project_id)
    return file_service.parse_center(db, project.id)


@router.get("/files/{file_id}/pages")
def list_pages(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    file = _get_file(db, file_id)
    return {
        "items": [
            {
                "page_index": page.page_index,
                "printed_page_number": page.printed_page_number,
                "parse_status": page.parse_status,
                "extraction_method": page.extraction_method,
                "note": page.note,
                "char_count": len(page.page_text or ""),
                "block_count": len(page.blocks),
            }
            for page in sorted(file.pages, key=lambda p: p.page_index)
        ],
        "total": len(file.pages),
    }


@router.get("/files/{file_id}/pages/{page_index}")
def get_page(
    file_id: str,
    page_index: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    file = _get_file(db, file_id)
    page = db.scalars(
        select(DocumentPage).where(
            DocumentPage.original_file_id == file.id,
            DocumentPage.page_index == page_index,
        )
    ).first()
    if not page:
        raise HTTPException(404, "页面不存在")
    return {
        "file_id": file.id,
        "file_name": file.file_name,
        "file_type": file.file_type,
        "page": page_payload(page),
    }


@router.get("/files/{file_id}/content")
def file_content(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_allow_query),
) -> FileResponse:
    file = _get_file(db, file_id)
    try:
        path = get_storage().local_path(file.storage_key)
    except FileNotFoundError:
        raise HTTPException(404, "原始文件已丢失")
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(file.file_type, "application/octet-stream"),
        headers={"Content-Disposition": "inline"},
    )


@router.get("/files/{file_id}/download")
def download_file(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_allow_query),
) -> FileResponse:
    file = _get_file(db, file_id)
    try:
        path = get_storage().local_path(file.storage_key)
    except FileNotFoundError:
        raise HTTPException(404, "原始文件已丢失")
    audit.log_audit(
        db,
        "file.download",
        "original_file",
        file.id,
        user,
        {"file_name": file.file_name},
        entity_name=file.file_name,
    )
    db.commit()
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(file.file_type, "application/octet-stream"),
        filename=file.file_name,
    )
