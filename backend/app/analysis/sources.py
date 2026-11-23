from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import DocumentPage, OriginalFile
from app.utils.text import compact_snippet

LOCATOR_PRECISION_BLOCK = "block"


@dataclass
class BlockSpan:
    """判定命中的统一定位接口：三个判定模块的命中项都只需给出 block 与偏移。

    以 dataclass 而非 mixin 提供：子类把 ``block/start/end`` 排在自身字段之后声明，
    这里只补一个 ``source`` 便捷属性，不参与字段顺序。
    """

    block: BlockRef
    start: int
    end: int

    @property
    def source(self) -> dict:
        return self.block.source(self.start, self.end)


def fold_span(offsets: list[int]):
    """返回把「折叠文本偏移」映射回原文的 span(start, end) 闭包。

    折叠（康熙部首 / 兼容汉字）逐字符 1:1，匹配偏移可原样映射回原文；
    没有 index_map 时退回 (0, 0)，调用方据此放弃该命中。
    """

    def span(start: int, end: int) -> tuple[int, int]:
        if not offsets:
            return 0, 0
        start = max(0, min(start, len(offsets) - 1))
        end = max(start + 1, min(end, len(offsets)))
        return offsets[start], offsets[end - 1] + 1

    return span


@dataclass
class BlockRef:
    company_id: str
    company_name: str
    file_id: str
    file_name: str
    file_type: str
    file_version: int
    page_index: int
    printed_page_number: str | None
    block_id: str
    block_index: int
    content: str
    normalized: str
    block_type: str
    # 该块文本的真实来源：native（PDF/DOCX 原生文本）或 ocr（扫描页识别）。
    # 证据链必须如实反映来源，否则复核人会把 OCR 结果当成原生文本。
    extraction_method: str = "native"

    def source(self, start: int | None = None, end: int | None = None) -> dict:
        """构造可回溯到原始文件的证据位置。start/end 为原文偏移量。"""
        if start is None:
            start = 0
        if end is None:
            end = len(self.content)
        start = max(0, min(start, len(self.content)))
        end = max(start, min(end, len(self.content)))
        snippet = compact_snippet(self.content[start:end], 300)
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "file_type": self.file_type,
            "file_version": self.file_version,
            "page_index": self.page_index,
            "printed_page_number": self.printed_page_number,
            "block_id": self.block_id,
            "block_index": self.block_index,
            "start_offset": start,
            "end_offset": end,
            "snippet": snippet,
            "bbox": None,
            # 当前解析器没有文本坐标时，至少保证页码/文本块可定位
            "locator_precision": LOCATOR_PRECISION_BLOCK,
            "extraction_method": self.extraction_method,
        }


def load_company_blocks(
    db: Session,
    company_id: str,
    company_name: str,
    file_ids: list[str],
    limit: int = 3000,
) -> list[BlockRef]:
    if not file_ids:
        return []

    files = {
        f.id: f
        for f in db.scalars(select(OriginalFile).where(OriginalFile.id.in_(file_ids))).all()
    }
    order = {file_id: index for index, file_id in enumerate(file_ids)}
    pages = list(
        db.scalars(
            select(DocumentPage)
            .options(selectinload(DocumentPage.blocks))
            .where(DocumentPage.original_file_id.in_(file_ids))
            .order_by(DocumentPage.page_index)
        )
    )
    pages.sort(key=lambda p: (order.get(p.original_file_id, 999), p.page_index))

    refs: list[BlockRef] = []
    for page in pages:
        file = files.get(page.original_file_id)
        if not file:
            continue
        for block in sorted(page.blocks, key=lambda b: b.block_index):
            if not block.content or not block.content.strip():
                continue
            refs.append(
                BlockRef(
                    company_id=company_id,
                    company_name=company_name,
                    file_id=file.id,
                    file_name=file.file_name,
                    file_type=file.file_type,
                    file_version=file.version,
                    page_index=page.page_index,
                    printed_page_number=page.printed_page_number,
                    block_id=block.id,
                    block_index=block.block_index,
                    content=block.content,
                    normalized=block.normalized_content,
                    block_type=block.block_type,
                    extraction_method=page.extraction_method or "native",
                )
            )
            if len(refs) >= limit:
                return refs
    return refs
