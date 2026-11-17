"""文档解析适配层.

V1 先支持可稳定提取文本的格式：
- PDF：pypdf 提取原生文本（坐标映射留了扩展位）
- DOCX：python-docx 提取段落
- TXT / MD：按文本读取

扫描件 OCR 通过 OCRProvider 接口接入（V1 为百度智能云 OCR），由调用方
（文件解析服务）按当前生效的「OCR 识别」规则构造识别器传入。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.models.enums import ExtractionMethod, PageStatus
from app.services.ocr import NoopOCRProvider, OCRProvider

_logger = logging.getLogger(__name__)


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


def parse_document(
    path: Path, file_type: str, ocr_provider: OCRProvider | None = None
) -> ParsedDocument:
    file_type = file_type.lower().lstrip(".")
    if file_type == "pdf":
        return _parse_pdf(path, ocr_provider)
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


def _parse_pdf(path: Path, ocr_provider: OCRProvider | None = None) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ParseError("缺少 PDF 解析依赖 pypdf") from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise ParseError(f"PDF 无法打开: {exc}") from exc

    pages: list[ParsedPage] = []
    ocr = ocr_provider or NoopOCRProvider()
    # 没有文本层的页先记下来，最后一次性交给 OCR：识别器可以据此把大文件拆成
    # 符合单次体积上限的多个文件逐份上传，避免整份大 PDF 被接口拒绝。
    pending: list[tuple[int, float | None, float | None]] = []
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
            pending.append((index, width, height))
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

    if pending:
        recognized: dict[int, str | None] = {}
        try:
            recognized = ocr.extract_pages(path, [item[0] for item in pending])
        except Exception as exc:  # OCR 整体失败不阻断文件解析
            recognized = {}
            _logger.warning("OCR 识别失败：%s", exc)
        for index, width, height in pending:
            ocr_text = (recognized.get(index) or "").strip()
            if ocr_text:
                pages.append(
                    ParsedPage(
                        page_index=index,
                        page_text=ocr_text,
                        extraction_method=ExtractionMethod.OCR.value,
                        width=width,
                        height=height,
                        note=f"{ocr.describe()} · 识别文本",
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
                        note=f"空白页或扫描页，{ocr.describe()}未识别到文本",
                    )
                )

    if not pages:
        raise ParseError("PDF 中没有可解析页面")
    pages.sort(key=lambda page: page.page_index)
    return ParsedDocument(pages=pages)


def _table_to_text(table) -> str:
    """把 DOCX 表格转成「一行一条、单元格用制表符分隔」的文本。

    报价表常以表格形式给出，而 python-docx 的 ``document.paragraphs`` 不包含
    表格内容——直接读段落会把整张报价表丢掉，价格维度只能靠正文里的落款句
    兜底。转成文本后，既进入文本相似度比较，也让报价正则有机会从表格里抽到
    总价（「投标总报价 | 9986000」这类一行一条的写法）。

    合并单元格在 python-docx 里会对同一格返回多次，这里按首次出现去重，
    避免把「序号/项目名称」这类跨列表头重复拼成噪声。
    """
    lines: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        seen: set[str] = set()
        for cell in row.cells:
            value = re.sub(r"\s+", " ", cell.text or "").strip()
            # 同一行里重复的单元格文本来自横向合并，只保留一份
            if not value or value in seen:
                continue
            seen.add(value)
            cells.append(value)
        if cells:
            lines.append("\t".join(cells))
    return "\n".join(lines)


def _parse_docx(path: Path) -> ParsedDocument:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover
        raise ParseError("缺少 DOCX 解析依赖 python-docx") from exc

    try:
        document = Document(str(path))
    except Exception as exc:
        raise ParseError(f"DOCX 无法打开: {exc}") from exc

    # 按 body 顺序遍历段落与表格：表格必须留在原来的位置，
    # 否则「表头在前、报价表在后」的文档会被打乱语境。
    pieces: list[str] = []
    table_count = 0
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, document).text
            if text and text.strip():
                pieces.append(text)
        elif tag == "tbl":
            table_text = _table_to_text(Table(child, document))
            if table_text:
                table_count += 1
                pieces.append(table_text)

    if not pieces:
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

    text = "\n".join(pieces)
    note = "DOCX 按单页组织；如需精确页码映射，建议上传 PDF"
    if table_count:
        note += f"；已提取 {table_count} 个表格"
    return ParsedDocument(
        pages=[
            ParsedPage(
                page_index=1,
                page_text=text,
                extraction_method=ExtractionMethod.NATIVE.value,
                note=note,
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
