from app.models import Company, DocumentBlock, DocumentPage, OriginalFile


def file_payload(file: OriginalFile, company: Company | None = None) -> dict:
    return {
        "id": file.id,
        "project_id": file.project_id,
        "company_id": file.company_id,
        "company_name": company.company_name if company else None,
        "kind": file.kind,
        "file_name": file.file_name,
        "file_type": file.file_type,
        "file_size": file.file_size,
        "file_hash": file.file_hash,
        "version": file.version,
        "page_count": file.page_count,
        "parse_status": file.parse_status,
        "parse_message": file.parse_message,
        "sort_order": file.sort_order,
        "is_active": file.is_active,
        "superseded_by_id": file.superseded_by_id,
        "uploaded_at": file.uploaded_at,
    }


def page_summary(file: OriginalFile) -> dict:
    pages = sorted(file.pages, key=lambda p: p.page_index)
    return {
        "id": file.id,
        "file_name": file.file_name,
        "file_type": file.file_type,
        "page_count": file.page_count,
        "success_pages": sum(1 for p in pages if p.parse_status == "success"),
        "empty_pages": sum(1 for p in pages if p.parse_status == "empty"),
        "failed_pages": sum(1 for p in pages if p.parse_status == "failed"),
        "ocr_pages": sum(1 for p in pages if p.extraction_method == "ocr"),
        "parse_status": file.parse_status,
        "parse_message": file.parse_message,
    }


def page_payload(page: DocumentPage) -> dict:
    return {
        "id": page.id,
        "page_index": page.page_index,
        "printed_page_number": page.printed_page_number,
        "extraction_method": page.extraction_method,
        "parse_status": page.parse_status,
        "page_text": page.page_text,
        "page_width": page.page_width,
        "page_height": page.page_height,
        "note": page.note,
        "blocks": [block_payload(block) for block in page.blocks],
    }


def block_payload(block: DocumentBlock) -> dict:
    return {
        "id": block.id,
        "block_index": block.block_index,
        "block_type": block.block_type,
        "content": block.content,
        "start_offset": block.start_offset,
        "end_offset": block.end_offset,
        "bbox": block.bbox,
        "is_body_text": block.is_body_text,
    }
