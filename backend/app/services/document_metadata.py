"""文档元数据指纹提取.

文本雷同可以被洗稿规避（换同义词、调语序、拆段），但「谁、用什么软件、
在哪台机器上做的这份文件」通常改不掉：

- **PDF**：``/Producer``、``/Creator``、``/Author``、``/CreationDate``、
  XMP 的 ``DocumentID``/``InstanceID``，以及 trailer 的 ``/ID``。
  ``/Producer`` 能反映导出工具与版本（同一台机器导出的一批文件往往完全一致），
  ``/ID`` 则是文件级别的唯一标识——两家企业提交同一份文件时它必然相同。
- **DOCX**：``core_properties`` 里的作者、最后修改人、创建/修改时间、公司名。
  同一编写者产出的多份标书，作者与最后修改人常常相同。

提取只读元数据，不解析正文，失败时返回空字典（不影响文件解析）。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pdf_id_hash(trailer) -> str | None:
    """把 PDF trailer 的 /ID 转成可读的短指纹。

    /ID 是二进制串，直接入库既不可读也容易在 JSON 里出问题；取两段 ID 的
    SHA-256 前 16 位，既能做等值比对也便于在界面上展示。
    """
    try:
        ids = trailer.get("/ID")
    except Exception:  # pragma: no cover - 损坏的 PDF
        return None
    if not ids:
        return None
    parts = []
    for item in ids:
        if isinstance(item, bytes):
            parts.append(item)
        elif isinstance(item, str):
            parts.append(item.encode("latin-1", "ignore"))
    if not parts:
        return None
    return hashlib.sha256(b"|".join(parts)).hexdigest()[:16]


def _xmp_value(xmp, name: str) -> str | None:
    try:
        return _clean(getattr(xmp, name, None))
    except Exception:  # pragma: no cover - XMP 解析异常
        return None


def _extract_pdf(path: Path) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    data: dict[str, str] = {}

    info = reader.metadata or {}
    data["producer"] = _clean(info.get("/Producer"))
    data["creator"] = _clean(info.get("/Creator"))
    data["author"] = _clean(info.get("/Author"))
    data["title"] = _clean(info.get("/Title"))
    data["subject"] = _clean(info.get("/Subject"))
    data["creation_date"] = _clean(info.get("/CreationDate"))
    data["modification_date"] = _clean(info.get("/ModDate"))

    pdf_id = _pdf_id_hash(getattr(reader, "trailer", None) or {})
    if pdf_id:
        data["pdf_id"] = pdf_id

    try:
        xmp = reader.xmp_metadata
    except Exception:  # pragma: no cover - XMP 解析异常
        xmp = None
    if xmp is not None:
        data["xmp_document_id"] = _xmp_value(xmp, "xmp_document_id")
        data["xmp_instance_id"] = _xmp_value(xmp, "xmp_instance_id")
        data.setdefault("author", _xmp_value(xmp, "dc_creator"))

    return {key: value for key, value in data.items() if value}


def _extract_docx(path: Path) -> dict:
    from docx import Document

    document = Document(str(path))
    props = document.core_properties
    data = {
        "author": _clean(getattr(props, "author", None)),
        "last_modified_by": _clean(getattr(props, "last_modified_by", None)),
        "title": _clean(getattr(props, "title", None)),
        "subject": _clean(getattr(props, "subject", None)),
    }
    created = getattr(props, "created", None)
    modified = getattr(props, "modified", None)
    if isinstance(created, datetime):
        data["creation_date"] = created.isoformat()
    if isinstance(modified, datetime):
        data["modification_date"] = modified.isoformat()
    return {key: value for key, value in data.items() if value}


def extract_metadata(path: Path, file_type: str) -> dict:
    """提取文档元数据；不支持或提取失败时返回空字典。"""
    file_type = (file_type or "").lower().lstrip(".")
    try:
        if file_type == "pdf":
            return _extract_pdf(path)
        if file_type in {"docx", "wps"}:
            return _extract_docx(path)
    except Exception as exc:  # 元数据缺失不应影响正文解析
        logger.info("元数据提取失败 %s: %s", path.name, exc)
    return {}
