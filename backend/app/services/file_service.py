from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    AnalysisTask,
    Company,
    DocumentBlock,
    DocumentPage,
    OriginalFile,
    Project,
    User,
)
from app.models.base import utcnow
from app.models.enums import (
    ExtractionMethod,
    FileKind,
    FileParseStatus,
    PageStatus,
)
from app.services import audit, logical_document_service, project_service
from app.services.parser import ParseError, parse_document
from app.storage.local import get_storage
from app.utils.text import normalize

CHUNK_SIZE = 1024 * 1024


class FileValidationError(Exception):
    pass


def _validate_upload(file_name: str, size: int) -> str:
    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix not in settings.allowed_extensions:
        raise FileValidationError(
            f"不支持的文件格式: {suffix or '未知'}，允许: {', '.join(sorted(settings.allowed_extensions))}"
        )
    if size <= 0:
        raise FileValidationError("文件为空")
    if size > settings.max_upload_size_mb * 1024 * 1024:
        raise FileValidationError(f"单个文件不能超过 {settings.max_upload_size_mb}MB")
    return suffix


async def _stream_to_temp(upload: UploadFile, max_size: int) -> tuple[str, int, str, str]:
    """流式落盘并计算哈希，避免把 500MB 文件读进内存。"""
    file_name = os.path.basename(upload.filename or "unnamed")
    suffix = Path(file_name).suffix.lower().lstrip(".")
    _validate_upload(file_name, 1)

    hasher = hashlib.sha256()
    size = 0
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{suffix}")
    try:
        while True:
            chunk = await upload.read(CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > max_size:
                raise FileValidationError(f"单个文件不能超过 {settings.max_upload_size_mb}MB")
            hasher.update(chunk)
            temp.write(chunk)
        temp.close()
    except Exception:
        temp.close()
        Path(temp.name).unlink(missing_ok=True)
        raise
    return temp.name, size, hasher.hexdigest(), file_name


def _next_sort_order(db: Session, company_id: str) -> int:
    current = db.scalar(
        select(func.max(OriginalFile.sort_order)).where(
            OriginalFile.company_id == company_id,
            OriginalFile.kind == FileKind.BID.value,
            OriginalFile.is_active.is_(True),
        )
    )
    return (current or 0) + 1


async def create_file(
    db: Session,
    project: Project,
    company: Company | None,
    kind: str,
    upload: UploadFile,
    actor: User | None = None,
    replaces: OriginalFile | None = None,
) -> OriginalFile:
    file_name = os.path.basename(upload.filename or "unnamed")
    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix not in settings.allowed_extensions:
        raise FileValidationError(f"不支持的文件格式: {suffix or '未知'}")

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    temp_path, size, file_hash, file_name = await _stream_to_temp(upload, max_bytes)
    _validate_upload(file_name, size)

    storage_key = f"{project.id}/{os.urandom(16).hex()}.{suffix}"
    try:
        get_storage().save_from_path(temp_path, storage_key)
    except Exception:
        Path(temp_path).unlink(missing_ok=True)
        raise

    version = 1
    previous = replaces
    if previous is None and company is not None:
        # 同名文件视为同一份文件的新版本，避免直接覆盖历史证据
        previous = db.scalars(
            select(OriginalFile)
            .where(
                OriginalFile.company_id == company.id,
                OriginalFile.file_name == file_name,
                OriginalFile.is_active.is_(True),
            )
            .order_by(OriginalFile.version.desc())
            .limit(1)
        ).first()
    if previous is not None:
        version = previous.version + 1

    record = OriginalFile(
        project_id=project.id,
        company_id=company.id if company else None,
        kind=kind,
        file_name=file_name,
        file_type=suffix,
        storage_key=storage_key,
        file_size=size,
        file_hash=file_hash,
        version=version,
        page_count=0,
        parse_status=FileParseStatus.PENDING.value,
        sort_order=_next_sort_order(db, company.id) if company else 0,
        is_active=True,
        uploaded_at=utcnow(),
    )
    db.add(record)
    db.flush()

    if previous is not None:
        previous.is_active = False
        previous.parse_status = FileParseStatus.SUPERSEDED.value
        previous.superseded_by_id = record.id
        if company is not None:
            company.confirmed_at = None
            logical_document_service.mark_logical_document_outdated(db, company)
            project_service.refresh_company_status(db, company)

    db.add(record)
    if company is not None:
        logical_document_service.mark_logical_document_outdated(db, company)
        company.confirmed_at = None
        project_service.refresh_company_status(db, company)

    audit.log_audit(
        db,
        "file.upload",
        "original_file",
        record.id,
        actor,
        {
            "file_name": file_name,
            "kind": kind,
            "company_id": company.id if company else None,
            "version": version,
            "size": size,
        },
    )
    project_service.refresh_project_status(db, project)
    db.commit()
    db.refresh(record)
    return record


async def replace_file(
    db: Session, file: OriginalFile, upload: UploadFile, actor: User | None = None
) -> OriginalFile:
    project = db.get(Project, file.project_id)
    company = db.get(Company, file.company_id) if file.company_id else None
    new_file = await create_file(
        db, project, company, file.kind, upload, actor=actor, replaces=file
    )
    audit.log_audit(
        db,
        "file.replace",
        "original_file",
        new_file.id,
        actor,
        {"file_name": new_file.file_name, "replaced_id": file.id},
        before={"file_id": file.id, "file_name": file.file_name, "version": file.version},
        after={"file_id": new_file.id, "file_name": new_file.file_name, "version": new_file.version},
        entity_name=new_file.file_name,
    )
    project_service.refresh_project_status(db, project)
    db.commit()
    return new_file


def delete_file(db: Session, file: OriginalFile, actor: User | None = None) -> None:
    file.is_active = False
    file.parse_status = FileParseStatus.SUPERSEDED.value
    company = db.get(Company, file.company_id) if file.company_id else None
    project = db.get(Project, file.project_id)
    if company:
        company.confirmed_at = None
        logical_document_service.mark_logical_document_outdated(db, company)
        logical_document_service.rebuild_logical_document(db, company)
        project_service.refresh_company_status(db, company)
    audit.log_audit(db, "file.delete", "original_file", file.id, actor, {"file_name": file.file_name})
    if project:
        project_service.refresh_project_status(db, project)
    db.commit()


def reorder_files(db: Session, company: Company, ordered_file_ids: list[str]) -> None:
    files = {f.id: f for f in logical_document_service.active_company_files(db, company.id)}
    if set(ordered_file_ids) != set(files):
        raise FileValidationError("排序列表必须包含该企业全部有效文件，且不能包含其他文件")
    for order, file_id in enumerate(ordered_file_ids):
        files[file_id].sort_order = order
    company.confirmed_at = None
    logical_document_service.rebuild_logical_document(db, company)
    project_service.refresh_company_status(db, company)
    db.commit()


def confirm_company(db: Session, company: Company, actor: User | None = None) -> Company:
    company.confirmed_at = utcnow()
    project_service.refresh_company_status(db, company)
    project = db.get(Project, company.project_id)
    if project:
        project_service.refresh_project_status(db, project)
    audit.log_audit(db, "company.confirm", "company", company.id, actor)
    db.commit()
    db.refresh(company)
    return company


def parse_file(
    db: Session,
    file_id: str,
    actor: User | None = None,
    auto_start: bool = True,
) -> OriginalFile:
    """同步解析单个文件；Celery 任务与演示种子数据共用此函数。

    auto_start 控制解析完成后是否自动发起两两比对（种子数据自行建任务，需关闭）。
    """
    file = db.get(OriginalFile, file_id)
    if file is None:
        raise FileValidationError("文件不存在")
    if not file.is_active:
        return file
    if file.parse_status == FileParseStatus.SUPERSEDED.value:
        return file

    file.parse_status = FileParseStatus.PARSING.value
    file.parse_message = None
    db.commit()

    company = db.get(Company, file.company_id) if file.company_id else None
    project = db.get(Project, file.project_id)

    # 重新解析前清空旧页面与文本块
    for page in list(file.pages):
        db.delete(page)
    db.flush()

    try:
        path = get_storage().local_path(file.storage_key)
        parsed = parse_document(path, file.file_type)
    except (ParseError, FileNotFoundError) as exc:
        _mark_parse_failed(db, file, company, project, str(exc), actor)
        _maybe_auto_start(db, project, auto_start)
        return file
    except Exception as exc:  # 未预期异常同样落到失败状态，便于重试
        _mark_parse_failed(db, file, company, project, f"解析异常: {exc}", actor)
        _maybe_auto_start(db, project, auto_start)
        return file

    success_pages = 0
    ocr_pages = 0
    empty_pages = 0
    failed_pages = 0
    for parsed_page in parsed.pages:
        blocks = []
        for block_index, block in enumerate(parsed_page.blocks):
            blocks.append(
                DocumentBlock(
                    block_index=block_index,
                    block_type=block.block_type,
                    content=block.content,
                    normalized_content=normalize(block.content),
                    start_offset=block.start_offset,
                    end_offset=block.end_offset,
                    bbox={"points": block.bbox} if block.bbox else None,
                    is_body_text=block.block_type != "heading",
                )
            )
        page = DocumentPage(
            original_file_id=file.id,
            page_index=parsed_page.page_index,
            printed_page_number=parsed_page.printed_page_number,
            extraction_method=parsed_page.extraction_method,
            parse_status=parsed_page.parse_status,
            page_text=parsed_page.page_text,
            page_width=parsed_page.width,
            page_height=parsed_page.height,
            note=parsed_page.note,
            blocks=blocks,
        )
        db.add(page)
        if parsed_page.parse_status == PageStatus.SUCCESS.value and parsed_page.page_text.strip():
            success_pages += 1
        elif parsed_page.extraction_method == ExtractionMethod.OCR.value:
            ocr_pages += 1
        elif parsed_page.parse_status == PageStatus.EMPTY.value:
            empty_pages += 1
        else:
            failed_pages += 1

    file.page_count = len(parsed.pages)
    if success_pages == 0:
        file.parse_status = FileParseStatus.FAILED.value
        file.parse_message = "没有成功提取任何页面文本"
    elif empty_pages or failed_pages:
        file.parse_status = FileParseStatus.PARTIAL.value
        file.parse_message = (
            f"成功 {success_pages}/{len(parsed.pages)} 页，"
            f"{empty_pages} 页空白/扫描页，{failed_pages} 页失败"
        )
    else:
        file.parse_status = FileParseStatus.SUCCESS.value
        file.parse_message = f"成功解析 {success_pages}/{len(parsed.pages)} 页"

    db.flush()
    if company:
        logical_document_service.rebuild_logical_document(db, company)
        project_service.refresh_company_status(db, company)
    if project:
        project_service.refresh_project_status(db, project)
    audit.log_audit(
        db,
        "file.parse",
        "original_file",
        file.id,
        actor,
        {"status": file.parse_status, "pages": file.page_count},
    )
    db.commit()
    db.refresh(file)
    _maybe_auto_start(db, project, auto_start)
    return file


def _maybe_auto_start(db: Session, project: Project | None, auto_start: bool) -> None:
    """解析全部完成后自动发起两两比对。"""
    if not auto_start or project is None:
        return
    from app.services import analysis_service  # 延迟导入，避免服务间循环依赖

    try:
        analysis_service.auto_start_if_ready(db, project)
    except Exception:  # 自动比对失败不影响解析结果落库
        db.rollback()
        logging.getLogger(__name__).exception("自动发起分析失败，项目 %s", project.id)


def _mark_parse_failed(
    db: Session,
    file: OriginalFile,
    company: Company | None,
    project: Project | None,
    message: str,
    actor: User | None,
) -> None:
    file.parse_status = FileParseStatus.FAILED.value
    file.parse_message = message
    file.page_count = 0
    db.flush()
    if company:
        project_service.refresh_company_status(db, company)
    if project:
        project_service.refresh_project_status(db, project)
    audit.log_audit(db, "file.parse_failed", "original_file", file.id, actor, {"message": message})
    db.commit()
    db.refresh(file)


def parse_center(db: Session, project_id: str) -> dict:
    files = list(
        db.scalars(
            select(OriginalFile)
            .where(OriginalFile.project_id == project_id)
            .order_by(OriginalFile.uploaded_at.desc())
        )
    )
    active = [f for f in files if f.is_active]
    counts = {status.value: 0 for status in FileParseStatus}
    for file in active:
        counts[file.parse_status] = counts.get(file.parse_status, 0) + 1

    rows = []
    for file in active:
        company = db.get(Company, file.company_id) if file.company_id else None
        pages = sorted(file.pages, key=lambda p: p.page_index)
        rows.append(
            {
                "id": file.id,
                "file_name": file.file_name,
                "file_type": file.file_type,
                "kind": file.kind,
                "company_id": file.company_id,
                "company_name": company.company_name if company else "招标文件",
                "page_count": file.page_count,
                "success_pages": sum(1 for p in pages if p.parse_status == PageStatus.SUCCESS.value),
                "ocr_pages": sum(1 for p in pages if p.extraction_method == ExtractionMethod.OCR.value),
                "empty_pages": sum(1 for p in pages if p.parse_status == PageStatus.EMPTY.value),
                "failed_pages": sum(1 for p in pages if p.parse_status == PageStatus.FAILED.value),
                "parse_status": file.parse_status,
                "parse_message": file.parse_message,
                "uploaded_at": file.uploaded_at,
                "version": file.version,
            }
        )

    last_task = db.scalars(
        select(AnalysisTask)
        .where(AnalysisTask.project_id == project_id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    ).first()

    return {
        "total": len(active),
        "counts": counts,
        "rows": rows,
        "analysis": {
            "task_id": last_task.id if last_task else None,
            "status": last_task.status if last_task else None,
            "started": last_task is not None,
        },
    }


def trigger_parse_for_project(db: Session, project_id: str) -> list[str]:
    """将项目中所有待解析/失败的文件排队解析。"""
    files = list(
        db.scalars(
            select(OriginalFile).where(
                OriginalFile.project_id == project_id,
                OriginalFile.is_active.is_(True),
                OriginalFile.parse_status.in_(
                    [
                        FileParseStatus.PENDING.value,
                        FileParseStatus.FAILED.value,
                        FileParseStatus.PARTIAL.value,
                    ]
                ),
            )
        )
    )
    return [f.id for f in files]
