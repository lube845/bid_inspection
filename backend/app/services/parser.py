"""文档解析适配层.

V1 先支持可稳定提取文本的格式：
- PDF：pypdf 提取原生文本（坐标映射留了扩展位）
- DOCX：python-docx 提取段落
- TXT / MD：按文本读取

扫描件 OCR 通过 OCRProvider 接口预留，真实模型接入后替换
`app.services.ocr.get_ocr_provider()` 即可，不影响解析流程。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.models.enums import ExtractionMethod, PageStatus
from app.services.ocr import get_ocr_provider


class ParseError(Exception):
    pass


@dataclass
class ParsedBlock:
    block_type: str
    content: str
    start_offset: int
    end_offset: int
    bbox: list[float] | None = None


@dataclass
class ParsedPage:
    page_index: int
    page_text: str
    extraction_method: str = ExtractionMethod.NATIVE.value
    parse_status: str = PageStatus.SUCCESS.value
    width: float | None = None
    height: float | None = None
    printed_page_number: str | None = None
    note: str | None = None
    blocks: list[ParsedBlock] = field(default_factory=list)


@dataclass
class ParsedDocument:
    pages: list[ParsedPage]
    note: str | None = None


def parse_document(path: Path, file_type: str) -> ParsedDocument:
    file_type = file_type.lower().lstrip(".")
    if file_type == "pdf":
        return _parse_pdf(path)
    if file_type == "docx":
        return _parse_docx(path)
    if file_type in {"txt", "md"}:
        return _parse_text(path)
    if file_type == "doc":
        raise ParseError("旧版 DOC 暂不支持解析，请转换为 DOCX 或 PDF 后重新上传")
    if file_type == "wps":
        # WPS 的 OOXML 兼容格式可以直接按 DOCX 读取；旧版二进制 WPS 仍提示转换
        try:
            return _parse_docx(path)
        except ParseError as exc:
            raise ParseError(
                "该 WPS 文件为旧版二进制格式，暂不支持直接解析，请导出为 PDF 或 DOCX 后上传"
            ) from exc
    raise ParseError(f"不支持的文件格式: {file_type}")


_MAX_BLOCK_CHARS = 800


def _split_blocks(text: str) -> list[ParsedBlock]:
    """按空行/长度切块，并保留相对原始 page_text 的字符偏移。"""
    blocks: list[ParsedBlock] = []
    buffer_start: int | None = None
    buffer_end = 0
    cursor = 0

    for line in text.splitlines(keepends=True):
        line_start = cursor
        cursor += len(line)
        stripped = line.strip()
        if not stripped:
            if buffer_start is not None and buffer_end > buffer_start:
                blocks.append(_make_block(text, buffer_start, buffer_end))
            buffer_start, buffer_end = None, 0
            continue
        if buffer_start is None:
            buffer_start = line_start
        buffer_end = line_start + len(line.rstrip("\r\n"))
        if buffer_end - buffer_start >= _MAX_BLOCK_CHARS:
            blocks.append(_make_block(text, buffer_start, buffer_end))
            buffer_start, buffer_end = None, 0

    if buffer_start is not None and buffer_end > buffer_start:
        blocks.append(_make_block(text, buffer_start, buffer_end))
    return blocks


def _make_block(text: str, start: int, end: int) -> ParsedBlock:
    content = text[start:end]
    looks_heading = len(content) <= 40 and bool(re.search(r"[：:]$", content.strip()))
    return ParsedBlock(
        block_type="heading" if looks_heading else "paragraph",
        content=content,
        start_offset=start,
        end_offset=end,
    )


def _parse_pdf(path: Path) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ParseError("缺少 PDF 解析依赖 pypdf") from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise ParseError(f"PDF 无法打开: {exc}") from exc

    pages: list[ParsedPage] = []
    ocr = get_ocr_provider()
    for index, pdf_page in enumerate(reader.pages, start=1):
        try:
            text = pdf_page.extract_text() or ""
        except Exception as exc:  # 单页失败不阻断整个文件
            pages.append(
                ParsedPage(
                    page_index=index,
                    page_text="",
                    extraction_method=ExtractionMethod.NONE.value,
                    parse_status=PageStatus.FAILED.value,
                    note=f"页面文本提取失败: {exc}",
                )
            )
            continue

        width = height = None
        try:
            box = pdf_page.mediabox
            width, height = float(box.width), float(box.height)
        except Exception:
            pass

        if not text.strip():
            ocr_text = ocr.extract_text(path, index)
            if ocr_text:
                pages.append(
                    ParsedPage(
                        page_index=index,
                        page_text=ocr_text,
                        extraction_method=ExtractionMethod.OCR.value,
                        width=width,
                        height=height,
                        note="OCR 识别",
                        blocks=_split_blocks(ocr_text),
                    )
                )
            else:
                pages.append(
                    ParsedPage(
                        page_index=index,
                        page_text="",
                        extraction_method=ExtractionMethod.NONE.value,
                        parse_status=PageStatus.EMPTY.value,
                        width=width,
                        height=height,
                        note="空白页或扫描页，OCR 未启用/未识别",
                    )
                )
            continue

        pages.append(
            ParsedPage(
                page_index=index,
                page_text=text,
                extraction_method=ExtractionMethod.NATIVE.value,
                width=width,
                height=height,
                blocks=_split_blocks(text),
            )
        )

    if not pages:
        raise ParseError("PDF 中没有可解析页面")
    return ParsedDocument(pages=pages)


def _parse_docx(path: Path) -> ParsedDocument:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover
        raise ParseError("缺少 DOCX 解析依赖 python-docx") from exc

    try:
        document = Document(str(path))
    except Exception as exc:
        raise ParseError(f"DOCX 无法打开: {exc}") from exc

    paragraphs = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    if not paragraphs:
        return ParsedDocument(
            pages=[
                ParsedPage(
                    page_index=1,
                    page_text="",
                    extraction_method=ExtractionMethod.NONE.value,
                    parse_status=PageStatus.EMPTY.value,
                    note="文档没有可提取文本，可能是纯图片文档",
                )
            ]
        )

    text = "\n".join(paragraphs)
    return ParsedDocument(
        pages=[
            ParsedPage(
                page_index=1,
                page_text=text,
                extraction_method=ExtractionMethod.NATIVE.value,
                note="DOCX 按单页组织；如需精确页码映射，建议上传 PDF",
                blocks=_split_blocks(text),
            )
        ]
    )


def _parse_text(path: Path) -> ParsedDocument:
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise ParseError("无法识别文本编码")

    chunks = text.split("\f") if "\f" in text else [text]
    pages = [
        ParsedPage(
            page_index=index,
            page_text=chunk,
            extraction_method=ExtractionMethod.NATIVE.value,
            parse_status=PageStatus.SUCCESS.value if chunk.strip() else PageStatus.EMPTY.value,
            blocks=_split_blocks(chunk),
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    return ParsedDocument(pages=pages)
