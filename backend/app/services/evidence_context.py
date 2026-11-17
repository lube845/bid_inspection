"""证据原文片段与上下文读取（比对分析页按需加载）。

比对分析页不再默认加载两侧完整原文，只展示：

- 命中相似片段本身；
- 命中片段前后必要的上下文（默认各 100 字，单侧合计 200 字）；
- 相似 / 差异的高亮标记；
- 文件、页码、章节等来源信息。

本模块只依赖证据记录里已有的原文区间（``source.ranges``），按需回读解析结果，
不保存大段上下文副本。上下文请求受服务端上限约束，避免一次拉回整份大文档。

坐标体系：所有偏移量都是「原始解析文本」里的字符坐标，即某个文本块
``DocumentBlock.content`` 内的下标，与 :meth:`TextChunk.original_ranges`
输出的坐标一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentBlock, DocumentPage, OriginalFile

# 默认上下文窗口：命中片段之前 / 之后各 100 字，单侧合计不超过 200 字
DEFAULT_BEFORE_CHARS = 100
DEFAULT_AFTER_CHARS = 100
# 用户主动「展开更多」时的步长
CONTEXT_STEP_CHARS = 200
# 服务端允许的单侧上下文上限，避免一次请求返回整份超大文档
MAX_CONTEXT_CHARS = 2000
# 上下文不足时最多向前 / 向后扩展的页数
MAX_CONTEXT_PAGES = 12

KIND_CONTEXT = "context"
KIND_MATCH = "match"
KIND_DIFF = "diff"


@dataclass
class BlockDoc:
    """解析结果里的一个文本块。"""

    id: str
    page_index: int
    block_index: int
    content: str
    block_type: str
    printed_page_number: str | None


@dataclass
class Piece:
    """落在同一个文本块内的一段原文。"""

    text: str
    page_index: int
    block_id: str
    start: int
    end: int


# --------------------------------------------------------------------------
# 解析结果读取
# --------------------------------------------------------------------------


def load_blocks(
    db: Session,
    file_id: str,
    page_from: int | None = None,
    page_to: int | None = None,
) -> list[BlockDoc]:
    """按页读取文本块，按 (页码, 块序号) 排序。"""
    query = (
        select(DocumentBlock, DocumentPage.page_index, DocumentPage.printed_page_number)
        .join(DocumentPage, DocumentPage.id == DocumentBlock.document_page_id)
        .where(DocumentPage.original_file_id == file_id)
    )
    if page_from is not None:
        query = query.where(DocumentPage.page_index >= page_from)
    if page_to is not None:
        query = query.where(DocumentPage.page_index <= page_to)
    rows = db.execute(
        query.order_by(DocumentPage.page_index, DocumentBlock.block_index)
    ).all()
    return [
        BlockDoc(
            id=block.id,
            page_index=page_index,
            block_index=block.block_index,
            content=block.content or "",
            block_type=block.block_type,
            printed_page_number=printed,
        )
        for block, page_index, printed in rows
    ]


class BlockReader:
    """一次请求内的文本块读取器，相同页窗口只查一次数据库。"""

    def __init__(self, db: Session):
        self.db = db
        self._blocks: dict[tuple[str, int, int], list[BlockDoc]] = {}
        self._page_counts: dict[str, int] = {}

    def blocks(
        self, file_id: str, page_from: int | None = None, page_to: int | None = None
    ) -> list[BlockDoc]:
        key = (
            file_id,
            page_from if page_from is not None else -1,
            page_to if page_to is not None else -1,
        )
        cached = self._blocks.get(key)
        if cached is None:
            cached = load_blocks(self.db, file_id, page_from, page_to)
            self._blocks[key] = cached
        return cached

    def page_count(self, file_id: str) -> int:
        if file_id not in self._page_counts:
            count = self.db.scalar(
                select(OriginalFile.page_count).where(OriginalFile.id == file_id)
            )
            self._page_counts[file_id] = int(count or 0)
        return self._page_counts[file_id]


# --------------------------------------------------------------------------
# 原文区间
# --------------------------------------------------------------------------


def source_ranges(source: dict | None) -> list[dict]:
    """从证据来源记录里取出命中片段在原文中的区间列表。"""
    if not source:
        return []
    ranges = [
        item
        for item in (source.get("ranges") or [])
        if item.get("block_id") and item.get("end_offset") is not None
    ]
    if ranges:
        return [
            {
                "block_id": item["block_id"],
                "page_index": item.get("page_index") or source.get("page_index"),
                "start": max(0, int(item.get("start_offset") or 0)),
                "end": max(0, int(item.get("end_offset") or 0)),
            }
            for item in ranges
        ]
    if source.get("block_id"):
        return [
            {
                "block_id": source["block_id"],
                "page_index": source.get("page_index"),
                "start": max(0, int(source.get("start_offset") or 0)),
                "end": max(0, int(source.get("end_offset") or 0)),
            }
        ]
    return []


def _matched_pieces(blocks: list[BlockDoc], ranges: list[dict]) -> list[Piece]:
    """按区间切出命中片段，并保留每个片段的页码与块内坐标。"""
    by_id = {block.id: block for block in blocks}
    pieces: list[Piece] = []
    for item in ranges:
        block = by_id.get(item["block_id"])
        if block is None:
            continue
        start = max(0, min(item["start"], len(block.content)))
        end = max(start, min(item["end"], len(block.content)))
        if end <= start:
            continue
        pieces.append(
            Piece(
                text=block.content[start:end],
                page_index=block.page_index,
                block_id=block.id,
                start=start,
                end=end,
            )
        )
    return pieces


def _walk_back(
    blocks: list[BlockDoc], first_range: dict, limit: int
) -> tuple[list[Piece], bool]:
    """向前（文档开头方向）收集上下文。

    返回 (片段列表, 是否已耗尽当前页窗口)。耗尽时说明窗口之外可能还有内容，
    需要继续扩大页窗口重读。
    """
    index = next(
        (i for i, block in enumerate(blocks) if block.id == first_range["block_id"]),
        None,
    )
    if index is None:
        return [], True
    collected: list[Piece] = []
    remaining = max(0, limit)
    block = blocks[index]
    head = block.content[: max(0, min(first_range["start"], len(block.content)))]
    if len(head) >= remaining:
        return (
            [
                Piece(
                    text=head[len(head) - remaining :] if remaining else "",
                    page_index=block.page_index,
                    block_id=block.id,
                    start=max(0, len(block.content) - remaining),
                    end=len(block.content),
                )
            ],
            False,
        )
    if head:
        collected.append(
            Piece(
                text=head,
                page_index=block.page_index,
                block_id=block.id,
                start=0,
                end=len(head),
            )
        )
    remaining -= len(head)
    cursor = index - 1
    while cursor >= 0 and remaining > 0:
        block = blocks[cursor]
        content = block.content
        if len(content) >= remaining:
            collected.append(
                Piece(
                    text=content[len(content) - remaining :],
                    page_index=block.page_index,
                    block_id=block.id,
                    start=len(content) - remaining,
                    end=len(content),
                )
            )
            remaining = 0
            cursor -= 1
            break
        collected.append(
            Piece(
                text=content,
                page_index=block.page_index,
                block_id=block.id,
                start=0,
                end=len(content),
            )
        )
        remaining -= len(content)
        cursor -= 1
    collected.reverse()
    return collected, (cursor < 0 and remaining > 0)


def _walk_forward(
    blocks: list[BlockDoc], last_range: dict, limit: int
) -> tuple[list[Piece], bool]:
    """向后（文档结尾方向）收集上下文。"""
    index = next(
        (i for i, block in enumerate(blocks) if block.id == last_range["block_id"]),
        None,
    )
    if index is None:
        return [], True
    collected: list[Piece] = []
    remaining = max(0, limit)
    block = blocks[index]
    tail = block.content[max(0, min(last_range["end"], len(block.content))) :]
    if len(tail) >= remaining:
        start = max(0, min(last_range["end"], len(block.content)))
        return (
            [
                Piece(
                    text=tail[:remaining],
                    page_index=block.page_index,
                    block_id=block.id,
                    start=start,
                    end=start + remaining,
                )
            ],
            False,
        )
    if tail:
        start = max(0, min(last_range["end"], len(block.content)))
        collected.append(
            Piece(
                text=tail,
                page_index=block.page_index,
                block_id=block.id,
                start=start,
                end=start + len(tail),
            )
        )
    remaining -= len(tail)
    cursor = index + 1
    while cursor < len(blocks) and remaining > 0:
        block = blocks[cursor]
        content = block.content
        if len(content) >= remaining:
            collected.append(
                Piece(
                    text=content[:remaining],
                    page_index=block.page_index,
                    block_id=block.id,
                    start=0,
                    end=remaining,
                )
            )
            remaining = 0
            cursor += 1
            break
        collected.append(
            Piece(
                text=content,
                page_index=block.page_index,
                block_id=block.id,
                start=0,
                end=len(content),
            )
        )
        remaining -= len(content)
        cursor += 1
    return collected, (cursor >= len(blocks) and remaining > 0)


_SENTENCE_BOUNDARIES = "。！？；;!?\n"


def _trim_to_boundary(piece: Piece, *, at_start: bool) -> Piece | None:
    """把上下文的截断点对齐到句子边界，避免从词语中间切开。

    只在边界距离截断点不超过 40 字时调整；否则保留原截断，避免丢太多上下文。
    """
    text = piece.text
    if not text:
        return None
    window = 40
    if at_start:
        for index in range(0, min(len(text), window)):
            if text[index] in _SENTENCE_BOUNDARIES and index + 1 < len(text):
                return Piece(
                    text=text[index + 1 :],
                    page_index=piece.page_index,
                    block_id=piece.block_id,
                    start=piece.start + index + 1,
                    end=piece.end,
                )
    else:
        for index in range(len(text) - 1, max(-1, len(text) - window - 1), -1):
            if text[index] in _SENTENCE_BOUNDARIES and index > 0:
                return Piece(
                    text=text[: index + 1],
                    page_index=piece.page_index,
                    block_id=piece.block_id,
                    start=piece.start,
                    end=piece.start + index + 1,
                )
    return piece


def _collect_context(
    reader: BlockReader,
    file_id: str,
    ranges: list[dict],
    before: int,
    after: int,
) -> tuple[list[Piece], list[Piece], bool, bool]:
    """读取命中片段前后的上下文。

    命中片段靠近文档开头或结尾时，只返回实际可获得的内容，不补造文本。
    """
    if not ranges:
        return [], [], False, False
    pages = [item["page_index"] for item in ranges if item.get("page_index")]
    if not pages:
        return [], [], False, False
    page_from = min(pages)
    page_to = max(pages)
    page_count = reader.page_count(file_id)
    window = 1
    before_pieces: list[Piece] = []
    after_pieces: list[Piece] = []
    before_exhausted = True
    after_exhausted = True
    while True:
        blocks = reader.blocks(
            file_id, max(1, page_from - window), page_to + window
        )
        before_pieces, before_exhausted = _walk_back(blocks, ranges[0], before)
        after_pieces, after_exhausted = _walk_forward(blocks, ranges[-1], after)
        can_grow_back = before_exhausted and page_from - window > 1
        can_grow_forward = after_exhausted and (
            not page_count or page_to + window < page_count
        )
        if (not can_grow_back and not can_grow_forward) or window >= MAX_CONTEXT_PAGES:
            break
        window += 1

    # 截断点尽量对齐到句子边界；如果对齐会显著缩短上下文就保留原样，
    # 保证默认窗口仍能拿到足够的前后文。
    if before_pieces and before:
        trimmed = _trim_to_boundary(before_pieces[0], at_start=True)
        if trimmed and len(trimmed.text) >= min(before, 100):
            before_pieces = [trimmed] + before_pieces[1:]
    if after_pieces and after:
        trimmed = _trim_to_boundary(after_pieces[-1], at_start=False)
        if trimmed and len(trimmed.text) >= min(after, 100):
            after_pieces = after_pieces[:-1] + [trimmed]

    has_more_before = bool(before_exhausted is False)
    has_more_after = bool(after_exhausted is False)
    return before_pieces, after_pieces, has_more_before, has_more_after


# --------------------------------------------------------------------------
# 相似 / 差异标记
# --------------------------------------------------------------------------


def _split_piece(piece: Piece, spans: list[tuple[int, int, str]]) -> list[tuple[Piece, str]]:
    """按局部区间把一个片段切成若干带标记的子片段。"""
    result: list[tuple[Piece, str]] = []
    cursor = 0
    length = len(piece.text)
    for start, end, kind in spans:
        if end <= start:
            continue
        if start > cursor:
            result.append(
                (
                    Piece(
                        text=piece.text[cursor:start],
                        page_index=piece.page_index,
                        block_id=piece.block_id,
                        start=piece.start + cursor,
                        end=piece.start + start,
                    ),
                    KIND_DIFF,
                )
            )
        result.append(
            (
                Piece(
                    text=piece.text[start:end],
                    page_index=piece.page_index,
                    block_id=piece.block_id,
                    start=piece.start + start,
                    end=piece.start + end,
                ),
                kind,
            )
        )
        cursor = end
    if cursor < length:
        result.append(
            (
                Piece(
                    text=piece.text[cursor:],
                    page_index=piece.page_index,
                    block_id=piece.block_id,
                    start=piece.start + cursor,
                    end=piece.end,
                ),
                KIND_DIFF,
            )
        )
    return result


def _classify_by_intervals(
    pieces: list[Piece], intervals: list[dict]
) -> list[tuple[Piece, str]]:
    """按算法记录的相似区间打标：区间内为相似，命中片段其余部分为差异。"""
    merged: dict[str, list[tuple[int, int]]] = {}
    for item in intervals:
        block_id = item.get("block_id")
        if not block_id:
            continue
        start = int(item.get("start") or 0)
        end = int(item.get("end") or 0)
        if end <= start:
            continue
        merged.setdefault(block_id, []).append((start, end))
    result: list[tuple[Piece, str]] = []
    for piece in pieces:
        spans = merged.get(piece.block_id, [])
        if not spans:
            result.append((piece, KIND_DIFF))
            continue
        spans.sort()
        local: list[tuple[int, int, str]] = []
        cursor = 0
        for start, end in spans:
            if end <= piece.start or start >= piece.end:
                continue
            left = max(start, piece.start) - piece.start
            right = min(end, piece.end) - piece.start
            if right <= left:
                continue
            if left > cursor:
                local.append((cursor, left, KIND_DIFF))
            local.append((left, right, KIND_MATCH))
            cursor = max(cursor, right)
        result.extend(_split_piece(piece, local))
    return result


def _classify_by_diff(
    pieces_a: list[Piece], pieces_b: list[Piece]
) -> tuple[list[tuple[Piece, str]], list[tuple[Piece, str]]]:
    """没有算法相似区间时的兜底：直接对比双方命中片段找出相同与不同之处。"""
    text_a = "".join(piece.text for piece in pieces_a)
    text_b = "".join(piece.text for piece in pieces_b)
    if not text_a and not text_b:
        return [], []
    matcher = SequenceMatcher(None, text_a, text_b, autojunk=False)
    labels_a: list[tuple[int, int, str]] = []
    labels_b: list[tuple[int, int, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            labels_a.append((i1, i2, KIND_MATCH))
            labels_b.append((j1, j2, KIND_MATCH))
        elif tag == "replace":
            labels_a.append((i1, i2, KIND_DIFF))
            labels_b.append((j1, j2, KIND_DIFF))
        elif tag == "delete":
            labels_a.append((i1, i2, KIND_DIFF))
        elif tag == "insert":
            labels_b.append((j1, j2, KIND_DIFF))
    return _apply_labels(pieces_a, labels_a), _apply_labels(pieces_b, labels_b)


def _apply_labels(
    pieces: list[Piece], labels: list[tuple[int, int, str]]
) -> list[tuple[Piece, str]]:
    """把全局区间上的标签落到各个原文片段上。"""
    result: list[tuple[Piece, str]] = []
    offset = 0
    for piece in pieces:
        start = offset
        end = offset + len(piece.text)
        offset = end
        spans: list[tuple[int, int, str]] = []
        for label_start, label_end, kind in labels:
            if label_end <= start or label_start >= end:
                continue
            left = max(label_start, start) - start
            right = min(label_end, end) - start
            if right > left:
                spans.append((left, right, kind))
        spans.sort()
        if not spans:
            result.append((piece, KIND_DIFF))
            continue
        result.extend(_split_piece(piece, spans))
    return result


def _to_segments(pieces: list[tuple[Piece, str]]) -> list[dict]:
    """合并相邻同类片段，输出可直接渲染的段落列表。"""
    segments: list[dict] = []
    for piece, kind in pieces:
        if not piece.text:
            continue
        if segments:
            last = segments[-1]
            if (
                last["kind"] == kind
                and last.get("page_index") == piece.page_index
                and last.get("block_id") == piece.block_id
                and last.get("end") == piece.start
            ):
                last["text"] += piece.text
                last["end"] = piece.end
                continue
        segments.append(
            {
                "kind": kind,
                "text": piece.text,
                "page_index": piece.page_index,
                "block_id": piece.block_id,
                "start": piece.start,
                "end": piece.end,
            }
        )
    return segments


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------


def _side_source(evidence, side: str) -> dict | None:
    """取证据某一侧的来源记录，优先使用参与方自己的定位信息。"""
    pair = evidence.pair
    source = evidence.source_a if side == "a" else evidence.source_b
    if pair is not None:
        company_id = pair.company_a_id if side == "a" else pair.company_b_id
        for participant in evidence.participants:
            if participant.company_id == company_id and participant.source:
                return participant.source
    return source


def _side_meta(source: dict | None, side: str) -> dict:
    source = source or {}
    return {
        "side": side,
        "company_id": source.get("company_id"),
        "company_name": source.get("company_name"),
        "file_id": source.get("file_id"),
        "file_name": source.get("file_name"),
        "file_type": source.get("file_type"),
        "file_version": source.get("file_version"),
        "page_index": source.get("page_index"),
        "page_start": source.get("page_start") or source.get("page_index"),
        "page_end": source.get("page_end") or source.get("page_index"),
        "printed_page_number": source.get("printed_page_number"),
        "chapter": source.get("chapter"),
        "section": source.get("section"),
        "content_type": source.get("content_type"),
        "locator_precision": source.get("locator_precision"),
    }


def _build_side(
    reader: BlockReader,
    source: dict | None,
    side: str,
    *,
    before: int,
    after: int,
    intervals: list[dict] | None,
    counterpart_pieces: list[Piece] | None,
    own_pieces: list[Piece] | None,
) -> dict:
    """构造单侧证据内容：命中片段 + 前后文 + 高亮标记。"""
    meta = _side_meta(source, side)
    ranges = source_ranges(source)
    if not ranges:
        # 没有原文区间时只回传摘要，不伪造上下文
        snippet = (source or {}).get("snippet") or ""
        meta.update(
            {
                "segments": (
                    [{"kind": KIND_MATCH, "text": snippet, "page_index": None, "block_id": None}]
                    if snippet
                    else []
                ),
                "ranges": [],
                "matched_chars": len(snippet),
                "diff_chars": 0,
                "context_before": 0,
                "context_after": 0,
                "has_more_before": False,
                "has_more_after": False,
                "context_available": False,
            }
        )
        return meta

    file_id = source.get("file_id")
    if not file_id:
        meta.update(
            {
                "segments": [],
                "ranges": [],
                "matched_chars": 0,
                "diff_chars": 0,
                "context_before": 0,
                "context_after": 0,
                "has_more_before": False,
                "has_more_after": False,
                "context_available": False,
            }
        )
        return meta
    before_pieces, after_pieces, more_before, more_after = _collect_context(
        reader, file_id, ranges, before, after
    )
    if own_pieces is not None:
        pieces = own_pieces
    else:
        pages = [item["page_index"] for item in ranges if item.get("page_index")]
        blocks = reader.blocks(file_id, min(pages), max(pages)) if pages else []
        pieces = _matched_pieces(blocks, ranges)

    if not pieces:
        # 原文区间指向的文本块已不可读（文件被替换或解析结果被清理），
        # 退回到证据里保存的摘要，至少保证审核人员能看到命中内容。
        snippet = (source or {}).get("snippet") or ""
        meta.update(
            {
                "segments": (
                    [{"kind": KIND_MATCH, "text": snippet, "page_index": None, "block_id": None}]
                    if snippet
                    else []
                ),
                "ranges": ranges,
                "matched_chars": len(snippet),
                "diff_chars": 0,
                "context_before": 0,
                "context_after": 0,
                "has_more_before": False,
                "has_more_after": False,
                "context_available": False,
            }
        )
        return meta

    if intervals:
        classified = _classify_by_intervals(pieces, intervals)
    elif counterpart_pieces is not None:
        classified, _ = _classify_by_diff(pieces, counterpart_pieces)
    else:
        classified = [(piece, KIND_MATCH) for piece in pieces]

    segments: list[dict] = []
    for piece in before_pieces:
        if not piece.text:
            continue
        segments.append(
            {
                "kind": KIND_CONTEXT,
                "text": piece.text,
                "page_index": piece.page_index,
                "block_id": piece.block_id,
                "start": piece.start,
                "end": piece.end,
            }
        )
    segments.extend(_to_segments(classified))
    for piece in after_pieces:
        if not piece.text:
            continue
        segments.append(
            {
                "kind": KIND_CONTEXT,
                "text": piece.text,
                "page_index": piece.page_index,
                "block_id": piece.block_id,
                "start": piece.start,
                "end": piece.end,
            }
        )

    matched_chars = sum(len(piece.text) for piece in pieces)
    diff_chars = sum(
        len(piece.text) for piece, kind in classified if kind == KIND_DIFF
    )
    meta.update(
        {
            "segments": segments,
            "ranges": ranges,
            # 命中片段与上下文的原文坐标，便于外部按偏移回溯原文
            "match_ranges": ranges,
            "context_ranges": [
                {
                    "block_id": piece.block_id,
                    "page_index": piece.page_index,
                    "start": piece.start,
                    "end": piece.end,
                }
                for piece in [*before_pieces, *after_pieces]
            ],
            "matched_chars": matched_chars,
            "diff_chars": diff_chars,
            "context_before": sum(len(piece.text) for piece in before_pieces),
            "context_after": sum(len(piece.text) for piece in after_pieces),
            "has_more_before": more_before,
            "has_more_after": more_after,
            "context_available": True,
        }
    )
    return meta


def _counterpart_pieces(reader: BlockReader, source: dict | None) -> list[Piece]:
    if not source:
        return []
    ranges = source_ranges(source)
    if not ranges:
        return []
    pages = [item["page_index"] for item in ranges if item.get("page_index")]
    if not pages:
        return []
    blocks = reader.blocks(source.get("file_id"), min(pages), max(pages))
    return _matched_pieces(blocks, ranges)


def build_sides(
    db: Session,
    evidence,
    *,
    before: int = DEFAULT_BEFORE_CHARS,
    after: int = DEFAULT_AFTER_CHARS,
    with_context: bool = True,
    reader: BlockReader | None = None,
) -> dict:
    """构造证据双方的对照内容。

    ``before`` / ``after`` 是命中片段前后的上下文字数，服务端会做上限约束。
    """
    before = max(0, min(int(before), MAX_CONTEXT_CHARS))
    after = max(0, min(int(after), MAX_CONTEXT_CHARS))
    reader = reader or BlockReader(db)
    data = evidence.evidence_data or {}
    source_a = _side_source(evidence, "a")
    source_b = _side_source(evidence, "b")
    intervals_a = data.get("match_intervals_a") or []
    intervals_b = data.get("match_intervals_b") or []

    ranges_a = source_ranges(source_a)
    ranges_b = source_ranges(source_b)
    pieces_a = _counterpart_pieces(reader, source_a) if ranges_a else []
    pieces_b = _counterpart_pieces(reader, source_b) if ranges_b else []

    side_a = _build_side(
        reader,
        source_a,
        "a",
        before=before if with_context else 0,
        after=after if with_context else 0,
        intervals=intervals_a,
        counterpart_pieces=pieces_b or None,
        own_pieces=pieces_a,
    )
    side_b = _build_side(
        reader,
        source_b,
        "b",
        before=before if with_context else 0,
        after=after if with_context else 0,
        intervals=intervals_b,
        counterpart_pieces=pieces_a or None,
        own_pieces=pieces_b,
    )
    diff = {
        "a_chars": side_a.get("diff_chars", 0),
        "b_chars": side_b.get("diff_chars", 0),
        "matched_chars": min(side_a.get("matched_chars", 0), side_b.get("matched_chars", 0)),
        "summary": _diff_summary(side_a, side_b),
    }
    return {"a": side_a, "b": side_b, "diff": diff}


def _diff_summary(side_a: dict, side_b: dict) -> str:
    a_diff = side_a.get("diff_chars") or 0
    b_diff = side_b.get("diff_chars") or 0
    if not a_diff and not b_diff:
        return "双方命中片段文字一致，未发现差异。"
    parts = []
    if a_diff:
        parts.append(f"投标人 A 侧有 {a_diff} 字与对方不一致")
    if b_diff:
        parts.append(f"投标人 B 侧有 {b_diff} 字与对方不一致")
    return "；".join(parts) + "，已用橙色标出。"


def build_side_context(
    db: Session,
    evidence,
    side: str,
    *,
    before: int = DEFAULT_BEFORE_CHARS,
    after: int = DEFAULT_AFTER_CHARS,
) -> dict:
    """单侧上下文读取，供「展开更多前文 / 后文」按需调用。"""
    if side not in {"a", "b"}:
        raise ValueError("side 必须是 a 或 b")
    before = max(0, min(int(before), MAX_CONTEXT_CHARS))
    after = max(0, min(int(after), MAX_CONTEXT_CHARS))
    reader = BlockReader(db)
    data = evidence.evidence_data or {}
    source = _side_source(evidence, side)
    other = _side_source(evidence, "b" if side == "a" else "a")
    intervals = data.get(f"match_intervals_{side}") or []
    counterpart = _counterpart_pieces(reader, other) or None
    payload = _build_side(
        reader,
        source,
        side,
        before=before,
        after=after,
        intervals=intervals,
        counterpart_pieces=counterpart,
        own_pieces=None,
    )
    return {
        "evidence_id": evidence.id,
        "side": side,
        "before_chars": before,
        "after_chars": after,
        "context_step": CONTEXT_STEP_CHARS,
        "max_context_chars": MAX_CONTEXT_CHARS,
        "side_payload": payload,
    }
