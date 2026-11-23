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


def page_payload(
    page: DocumentPage,
    *,
    block_offset: int = 0,
    block_limit: int | None = None,
) -> dict:
    """页面详情。

    大文件（例如 50MB 纯文本）单页可能有上万个文本块，一次全量返回会长时间
    占用连接甚至超时。这里支持按块分页，同时返回 ``block_total`` 供前端
    判断是否还有后续内容。
    """
    blocks = sorted(page.blocks, key=lambda b: b.block_index)
    total = len(blocks)
    start = max(0, block_offset)
    end = total if block_limit is None else start + max(0, block_limit)
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
        "blocks": [block_payload(block) for block in blocks[start:end]],
        "block_offset": start,
        "block_total": total,
        "has_more_blocks": end < total,
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
