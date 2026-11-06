import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    LogicalDocument,
    LogicalDocumentItem,
    LogicalDocumentVersion,
    OriginalFile,
)
from app.models.enums import FileKind, FileParseStatus, LogicalDocStatus


def active_company_files(db: Session, company_id: str) -> list[OriginalFile]:
    return list(
        db.scalars(
            select(OriginalFile)
            .where(
                OriginalFile.company_id == company_id,
                OriginalFile.kind == FileKind.BID.value,
                OriginalFile.is_active.is_(True),
            )
            .order_by(OriginalFile.sort_order, OriginalFile.uploaded_at)
        )
    )


def mark_logical_document_outdated(db: Session, company: Company) -> None:
    if company.logical_document:
        company.logical_document.status = LogicalDocStatus.OUTDATED.value


def rebuild_logical_document(db: Session, company: Company) -> LogicalDocumentVersion | None:
    """根据当前有效文件与人工排序生成新的逻辑标书版本（内容未变则不新增版本）。"""
    files = active_company_files(db, company.id)
    doc = company.logical_document
    if doc is None:
        doc = LogicalDocument(project_id=company.project_id, company_id=company.id)
        db.add(doc)
        db.flush()
        company.logical_document = doc

    if not files:
        doc.status = LogicalDocStatus.NOT_GENERATED.value
        doc.current_version_id = None
        db.flush()
        return None

    signature = "|".join(
        f"{f.id}:{f.version}:{f.file_hash}:{f.sort_order}:{f.page_count}" for f in files
    )
    content_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest()

    current = None
    if doc.current_version_id:
        current = db.get(LogicalDocumentVersion, doc.current_version_id)
    if current and current.content_hash == content_hash:
        doc.status = LogicalDocStatus.GENERATED.value
        db.flush()
        return current

    latest_version_no = max((v.version_no for v in doc.versions), default=0)
    version = LogicalDocumentVersion(
        logical_document_id=doc.id,
        version_no=latest_version_no + 1,
        total_pages=sum(f.page_count for f in files),
        content_hash=content_hash,
        status="generated",
    )
    db.add(version)
    db.flush()

    for order, file in enumerate(files):
        db.add(
            LogicalDocumentItem(
                logical_document_version_id=version.id,
                original_file_id=file.id,
                sort_order=order,
                section_name=file.file_name,
            )
        )
    doc.current_version_id = version.id
    doc.status = LogicalDocStatus.GENERATED.value
    db.flush()
    return version


def logical_document_preview(db: Session, company: Company, include_pages: bool = True) -> dict:
    doc = company.logical_document
    if not doc or not doc.current_version_id:
        return {
            "company_id": company.id,
            "company_name": company.company_name,
            "version_no": None,
            "status": doc.status if doc else LogicalDocStatus.NOT_GENERATED.value,
            "total_pages": 0,
            "items": [],
        }

    version = db.get(LogicalDocumentVersion, doc.current_version_id)
    items_payload = []
    if version:
        items = list(
            db.scalars(
                select(LogicalDocumentItem)
                .where(LogicalDocumentItem.logical_document_version_id == version.id)
                .order_by(LogicalDocumentItem.sort_order)
            )
        )
        for item in items:
            file = db.get(OriginalFile, item.original_file_id)
            if not file:
                continue
            pages = []
            if include_pages:
                pages = [
                    {
                        "page_index": page.page_index,
                        "printed_page_number": page.printed_page_number,
                        "parse_status": page.parse_status,
                        "extraction_method": page.extraction_method,
                    }
                    for page in sorted(file.pages, key=lambda p: p.page_index)
                ]
            items_payload.append(
                {
                    "file_id": file.id,
                    "file_name": file.file_name,
                    "file_type": file.file_type,
                    "page_count": file.page_count,
                    "parse_status": file.parse_status,
                    "sort_order": item.sort_order,
                    "pages": pages,
                }
            )

    return {
        "company_id": company.id,
        "company_name": company.company_name,
        "version_id": version.id if version else None,
        "version_no": version.version_no if version else None,
        "status": doc.status,
        "total_pages": version.total_pages if version else 0,
        "items": items_payload,
    }


def count_unparsed_files(db: Session, company_id: str) -> int:
    files = active_company_files(db, company_id)
    return sum(
        1
        for f in files
        if f.parse_status
        not in {FileParseStatus.SUCCESS.value, FileParseStatus.PARTIAL.value}
    )
