"""结构化文本分块（方案第 4、6 章）.

把解析器产出的「页面级文本块」重新组织成用于相似度比较的 ``TextChunk``：

- 逐行识别章节标题，标题只作为结构元数据，不进入正文 Shingling；
- 以自然段落为最小单元，短段落同章节合并，长段落按句子边界拆分；
- 目标长度 300～500 字，最大 800 字，最小有效长度 50 字；
- 每个 Chunk 保留到原始文件、物理页码、原文偏移量的映射；
- 过滤页码、页眉页脚等重复噪声。

Chunk 不跨文件、不跨章节合并，保证证据可以稳定定位回原文。

标题识别分两档，避免把正文编号条目误判成标题后丢掉内容：

- **明确标题**（``第X章``/``第X节``/``一、``/``3.1``）：从正文中剥离，只保留结构元数据；
- **歧义编号**（``1.``/``1、``/``（一）``）：既可能是小节标题，也可能是正文里的编号条目，
  一律保留在正文中，只在足够短时登记为小节标签。宁可正文里多留几个字，也不能丢掉
  可能暴露串标的实施条款。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

from app.analysis.sources import BlockRef
from app.utils.text import compact_snippet, normalize_with_map

# 结构识别规则 -------------------------------------------------------------

# 一级标题：第X章 / 第X篇 / 第X部，形态明确
_CHAPTER = re.compile(r"^第\s*[一二三四五六七八九十百零〇0-9]+\s*[章篇部]")
# 二级标题：第X节
_CHAPTER_SECTION = re.compile(r"^第\s*[一二三四五六七八九十百零〇0-9]+\s*[节]")
# 一、二、三 级标题
_LEVEL1 = re.compile(r"^[一二三四五六七八九十]+\s*[、.．]\s*\S")
# 3.1 / 3.1.2 形式的编号标题
_LEVEL2 = re.compile(r"^\d+(?:\.\d+)+\s*\S")
# 1. / 1、 形式的编号：既可能是小节标题，也可能是正文编号条目
_LEVEL2_AMBIGUOUS = re.compile(r"^(?:\d+\s*[、．]\s*\S|\d+\.(?!\d)\s*\S)")
# （一）/（1）形式的编号：歧义同上
_LEVEL3_AMBIGUOUS = re.compile(r"^[（(]\s*[一二三四五六七八九十\d]{1,3}\s*[）)]\s*\S")
# 编号条目：用于判断「新段落开始」
_LIST_START = re.compile(
    r"^(?:\d+\s*[、．]\s|\d+\.(?!\d)\s|[（(]\s*[一二三四五六七八九十\d]{1,3}\s*[）)]\s)"
)
# 中文标书常见的行内标题写法：「一、项目实施方案。正文……」
# 只收录形态明确的标记，避免把「（一）严格执行…」这类正文条目当成标题剥离掉。
_INLINE_HEADING_MARKER = re.compile(
    r"^[ \t　]*(?:"
    r"第\s*[一二三四五六七八九十百零〇0-9]+\s*[章篇部节]"
    r"|[一二三四五六七八九十]+\s*[、．]"
    r"|\d+(?:\.\d+)+"
    r")\s*"
)
_PAGE_NOISE = re.compile(
    r"^(?:第\s*\d+\s*页(?:\s*[/,，]?\s*共\s*\d+\s*页)?|共\s*\d+\s*页|[-—–]\s*\d+\s*[-—–]|\d{1,4})$"
)
_SENTENCE_END = re.compile(r"[。！？；!?;]")

# 单行标题的最大规范化长度
_MAX_HEADING_CHARS = 40
# 编号标题（3.1 形式）判定为标题的最大长度
_MAX_NUMBERED_HEADING_CHARS = 20
# 歧义编号登记为小节标签的最大长度
_MAX_LABEL_CHARS = 12
# 归一化后包含这些标点即认为是一句话而不是标题
_SENTENCE_MARKS = ("。", "；", "！", "？", "，", "!", "?", ";")
_TERMINAL_MARKS = ("。", "；", "！", "？", "，", "!", "?", ";", "、", "：", ":")
# 行内标题的标题文字最大长度
_MAX_INLINE_TITLE_CHARS = 20

# 内容类型关键词（用于选择处理策略，不改变核心算法）
_TYPE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("personnel", ("人员", "团队", "组织机构", "项目班子", "项目经理", "负责人")),
    ("price", ("报价", "价格", "费用", "清单", "造价", "开标一览")),
    ("commercial", ("商务", "资格", "资质", "响应", "承诺函", "投标函")),
    ("company_profile", ("企业介绍", "公司简介", "企业简介", "业绩", "案例", "荣誉")),
    ("technical", ("技术", "方案", "实施", "施工", "服务", "质量", "安全", "工艺", "进度")),
]


@dataclass
class ChunkSegment:
    """Chunk 内的一个原文片段，保持到原始 Block 的偏移映射。"""

    block: BlockRef
    start: int
    end: int
    normalized: str
    index_map: list[int]

    @property
    def content(self) -> str:
        return self.block.content[self.start : self.end]


class _Heading(NamedTuple):
    level: str
    label: str
    strip: bool  # True 表示该行是标题，不进入正文


class _Line(NamedTuple):
    block: BlockRef
    start: int  # 在 block.content 内的偏移
    end: int
    text: str  # 去掉行尾空白后的原始文本
    indented: bool  # 是否带缩进（PDF 段落起始的常见信号）
    heading: tuple[str, str] | None = None  # 已知标题 (level, label)


@dataclass
class TextChunk:
    chunk_id: str
    company_id: str
    company_name: str
    file_id: str
    file_name: str
    file_type: str
    file_version: int
    chunk_index: int
    content_type: str
    chapter: str
    section: str
    segments: list[ChunkSegment] = field(default_factory=list)
    text: str = ""
    normalized: str = ""

    @property
    def page_start(self) -> int:
        return min(segment.block.page_index for segment in self.segments)

    @property
    def page_end(self) -> int:
        return max(segment.block.page_index for segment in self.segments)

    @property
    def block_ids(self) -> list[str]:
        return list(dict.fromkeys(segment.block.block_id for segment in self.segments))

    @property
    def first_block(self) -> BlockRef:
        return self.segments[0].block

    def _locate(self, offset: int) -> tuple[ChunkSegment, int]:
        """规范化偏移量 -> (片段, 片段内偏移)。"""
        offset = max(0, min(offset, max(0, len(self.normalized) - 1)))
        cursor = 0
        for segment in self.segments:
            size = len(segment.normalized)
            if offset < cursor + size:
                return segment, offset - cursor
            cursor += size
        return self.segments[-1], len(self.segments[-1].normalized)

    def _iter_original(self, start: int, end: int):
        """遍历 [start, end) 规范化区间覆盖的原文片段。"""
        start = max(0, min(start, len(self.normalized)))
        end = max(start, min(end, len(self.normalized)))
        cursor = 0
        for segment in self.segments:
            size = len(segment.normalized)
            seg_start = max(start, cursor)
            seg_end = min(end, cursor + size)
            if seg_start < seg_end:
                local_start = seg_start - cursor
                local_end = seg_end - cursor
                orig_start = segment.index_map[local_start]
                orig_end = segment.index_map[local_end - 1] + 1
                yield segment, orig_start, orig_end
            cursor += size

    def original_ranges(self, start: int, end: int) -> list[dict]:
        """把规范化区间转换为原文区间列表，用于展示排除范围。"""
        ranges: list[dict] = []
        for segment, orig_start, orig_end in self._iter_original(start, end):
            ranges.append(
                {
                    "start": orig_start,
                    "end": orig_end,
                    "block_id": segment.block.block_id,
                    "page_index": segment.block.page_index,
                    "file_id": segment.block.file_id,
                }
            )
        return ranges

    def source(self, start: int | None = None, end: int | None = None) -> dict:
        """把规范化偏移量映射回原始文件位置，并附带章节信息。"""
        if start is None:
            start = 0
        if end is None:
            end = len(self.normalized)
        pieces: list[str] = []
        ranges: list[dict] = []
        remaining = 300
        first_segment = None
        first_orig_start = first_orig_end = 0
        for segment, orig_start, orig_end in self._iter_original(start, end):
            if first_segment is None:
                first_segment = segment
                first_orig_start, first_orig_end = orig_start, orig_end
            ranges.append(
                {
                    "block_id": segment.block.block_id,
                    "page_index": segment.block.page_index,
                    "file_id": segment.block.file_id,
                    "start_offset": orig_start,
                    "end_offset": orig_end,
                }
            )
            piece = segment.block.content[orig_start:orig_end]
            if piece and remaining > 0:
                pieces.append(piece)
                remaining -= len(piece)
        if first_segment is None:
            return self.segments[0].block.source()
        source = first_segment.block.source(first_orig_start, first_orig_end)
        source.update(
            {
                "snippet": compact_snippet("\n".join(pieces), 300),
                "ranges": ranges,
                "chapter": self.chapter,
                "section": self.section,
                "content_type": self.content_type,
                "chunk_id": self.chunk_id,
                "chunk_index": self.chunk_index,
                "chunk_block_ids": self.block_ids,
                "page_start": min(item["page_index"] for item in ranges),
                "page_end": max(item["page_index"] for item in ranges),
            }
        )
        return source


def _looks_like_sentence(normalized: str) -> bool:
    """归一化文本是否像一句话（而不是标题）。"""
    if normalized.endswith(_TERMINAL_MARKS):
        return True
    return any(mark in normalized for mark in _SENTENCE_MARKS)


def _split_inline_heading(text: str) -> tuple[str, str, int] | None:
    """拆分中文标书常见的行内标题写法。

    例如 ``一、项目实施方案。我方将严格按照招标文件要求……``：
    前半段是标题，后半段是正文。返回 ``(level, label, split_at)``，
    ``split_at`` 是正文在原行中的起始位置。

    只有形态明确、且标题文字不含数字与逗号的标记才会被拆分，避免把
    「1. 本公司针对本项目建立完善的质量管理体系，配备专职质量管理人员。」
    这类正文句子当成标题剥离掉。
    """
    match = _INLINE_HEADING_MARKER.match(text)
    if not match:
        return None
    marker = match.group(0)
    rest = text[match.end() :]
    end = rest.find("。")
    if end <= 0:
        return None
    title = rest[:end]
    if not title.strip() or len(title) > _MAX_INLINE_TITLE_CHARS:
        return None
    if any(char.isdigit() for char in title):
        return None
    if any(char in title for char in "，、；：,;:"):
        return None

    label = (marker + title).strip()
    if marker.lstrip().startswith("第"):
        level = "chapter" if marker.rstrip()[-1] in "章篇部" else "section"
    elif marker.lstrip()[0].isdigit():
        level = "section"
    else:
        level = "chapter"
    return level, label, match.end() + end + 1


def _is_body_line(text: str) -> bool:
    """判断一行是否是正文（用于编号标题的上下文确认）。"""
    stripped = text.strip()
    if not stripped:
        return False
    normalized, _ = normalize_with_map(stripped)
    if not normalized:
        return False
    if len(normalized) > _MAX_HEADING_CHARS:
        return True
    if _looks_like_sentence(normalized):
        return True
    # 只有明确的章节标题才说明「下一行不是正文」
    if _CHAPTER.match(stripped) or _CHAPTER_SECTION.match(stripped) or _LEVEL1.match(stripped):
        return False
    return True


def _classify_line(
    text: str, next_text: str | None, block_type: str = "paragraph"
) -> _Heading | None:
    """判断一行是否为章节标题。

    ``next_text`` 是同一文件中的下一行，用于确认「3.1 xxx」这类编号标题后面
    确实跟着正文，避免把正文句子当成标题剥离掉。
    """
    stripped = text.strip()
    if not stripped:
        return None
    normalized, _ = normalize_with_map(stripped)
    if not normalized or len(normalized) > _MAX_HEADING_CHARS:
        return None
    if _looks_like_sentence(normalized):
        return None

    if _CHAPTER.match(stripped):
        return _Heading("chapter", stripped, True)
    if _CHAPTER_SECTION.match(stripped):
        return _Heading("section", stripped, True)
    if _LEVEL1.match(stripped):
        return _Heading("chapter", stripped, True)
    if _LEVEL2.match(stripped):
        # 编号标题必须够短，且后面紧跟正文，否则宁可留在正文里
        if (
            len(normalized) <= _MAX_NUMBERED_HEADING_CHARS
            and next_text is not None
            and _is_body_line(next_text)
        ):
            return _Heading("section", stripped, True)
        return None
    if block_type == "heading":
        return _Heading("section", stripped, True)
    # 歧义编号：保留在正文中，只在足够短时登记为小节标签
    if _LEVEL2_AMBIGUOUS.match(stripped) or _LEVEL3_AMBIGUOUS.match(stripped):
        if len(normalized) <= _MAX_LABEL_CHARS:
            return _Heading("section", stripped, False)
        return None
    if stripped.endswith(("：", ":")) and len(normalized) <= 20:
        return _Heading("section", stripped, False)
    return None


def detect_heading(content: str, block_type: str = "paragraph") -> tuple[str, str] | None:
    """识别章节标题。返回 (level, 标题文本)，level 为 chapter/section。

    保留这个函数是为了兼容「整块就是一个标题」的旧调用方式；正文标题识别
    走的是逐行的 :func:`_classify_line`。这里没有上下文可参考时，对
    ``3.1 xxx`` 这类编号标题采取更宽松的判定。
    """
    lines = [line for line in (content or "").splitlines() if line.strip()]
    if not lines:
        return None
    heading = _classify_line(
        lines[0], lines[1] if len(lines) > 1 else None, block_type
    )
    if heading is not None:
        return heading.level, heading.label
    # 无上下文时，形态明确的编号标题仍然判定为小节标题
    stripped = lines[0].strip()
    normalized, _ = normalize_with_map(stripped)
    if (
        normalized
        and len(normalized) <= _MAX_NUMBERED_HEADING_CHARS
        and not _looks_like_sentence(normalized)
        and _LEVEL2.match(stripped)
    ):
        return "section", stripped
    return None


def classify_content(chapter: str, section: str) -> str:
    text = f"{chapter}{section}"
    for content_type, keywords in _TYPE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return content_type
    return "general"


def _is_noise(normalized: str, raw: str) -> bool:
    if not normalized:
        return True
    if _PAGE_NOISE.match(raw.strip()):
        return True
    return False


def _iter_lines(content: str):
    """把块内文本拆成行，产出 (start, end, text, indented)。"""
    pos = 0
    total = len(content)
    while pos <= total:
        newline = content.find("\n", pos)
        end = total if newline == -1 else newline
        raw = content[pos:end].rstrip("\r")
        if raw.strip():
            yield pos, pos + len(raw), raw, raw[:1] in (" ", "\t", "　")
        if newline == -1:
            break
        pos = newline + 1


def _split_spans(content: str, max_chars: int, target_min: int) -> list[tuple[int, int]]:
    """把超长段落按句子边界拆分，返回原文偏移区间。"""
    if len(content) <= max_chars:
        return [(0, len(content))]

    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(content):
        hard_end = min(len(content), cursor + max_chars)
        if hard_end >= len(content):
            spans.append((cursor, len(content)))
            break
        # 在 [target_min, max_chars] 内找最后一个句子结束符
        window = content[cursor:hard_end]
        cut = None
        for match in _SENTENCE_END.finditer(window):
            position = match.end()
            if position >= target_min:
                cut = cursor + position
        if cut is None or cut <= cursor:
            # 没有合适句末：优先保留完整句子；仍然没有则硬切
            first = _SENTENCE_END.search(window, target_min)
            cut = cursor + first.end() if first else hard_end
        spans.append((cursor, cut))
        cursor = cut
    return spans


def _make_segment(block: BlockRef, start: int, end: int) -> ChunkSegment:
    text = block.content[start:end]
    normalized, index_map = normalize_with_map(text)
    return ChunkSegment(
        block=block,
        start=start,
        end=end,
        normalized=normalized,
        index_map=[start + offset for offset in index_map],
    )


def _new_chunk(
    segments: list[ChunkSegment],
    chapter: str,
    section: str,
    content_type: str,
    index: int,
) -> TextChunk:
    first = segments[0].block
    return TextChunk(
        chunk_id=f"{first.company_id}:{first.file_id}:{first.block_id}:{index}",
        company_id=first.company_id,
        company_name=first.company_name,
        file_id=first.file_id,
        file_name=first.file_name,
        file_type=first.file_type,
        file_version=first.file_version,
        chunk_index=index,
        content_type=content_type,
        chapter=chapter,
        section=section,
        segments=segments,
        text="\n".join(segment.content for segment in segments),
        normalized="".join(segment.normalized for segment in segments),
    )


def _expand_plain_line(
    block: BlockRef,
    start: int,
    end: int,
    text: str,
    indented: bool,
    max_chars: int,
    target_min: int,
) -> list[_Line]:
    """一行普通正文；超长时按句子边界再切。"""
    if len(text) <= max_chars:
        return [_Line(block, start, end, text, indented)]
    lines: list[_Line] = []
    for span_start, span_end in _split_spans(text, max_chars, target_min):
        piece = text[span_start:span_end]
        if not piece.strip():
            continue
        lines.append(
            _Line(
                block,
                start + span_start,
                start + span_end,
                piece,
                indented and span_start == 0,
            )
        )
    return lines


def _expand_lines(block: BlockRef, max_chars: int, target_min: int) -> list[_Line]:
    """把一个块展开成行序列，并识别行内标题。"""
    lines: list[_Line] = []
    for start, end, text, indented in _iter_lines(block.content):
        normalized, _ = normalize_with_map(text)
        if _is_noise(normalized, text):
            continue
        inline = _split_inline_heading(text)
        if inline is not None:
            level, label, split_at = inline
            lines.append(
                _Line(block, start, start + split_at, text[:split_at], indented, (level, label))
            )
            remainder = text[split_at:]
            if remainder.strip():
                lines.extend(
                    _expand_plain_line(
                        block, start + split_at, end, remainder, False, max_chars, target_min
                    )
                )
            continue
        lines.extend(
            _expand_plain_line(block, start, end, text, indented, max_chars, target_min)
        )
    return lines


def build_chunks(blocks: list[BlockRef], rules: dict) -> list[TextChunk]:
    """按章节 + 段落 + 长度规则生成文本块。"""
    target_min = int(rules.get("text_chunk_target_min", 300))
    target_max = int(rules.get("text_chunk_target_max", 500))
    max_chars = max(int(rules.get("text_chunk_max_chars", 800)), target_max)
    min_chars = int(rules.get("text_chunk_min_chars", 50))

    # 先按行展开：解析器产出的块常常是整页文本，段落结构要在行粒度上还原。
    # 行按文件归拢，这样「下一行」的上下文判断可以跨块、跨页延续。
    file_lines: dict[str, list[_Line]] = {}
    file_order: list[str] = []
    for block in blocks:
        lines = _expand_lines(block, max_chars, target_min)
        if not lines:
            continue
        if block.file_id not in file_lines:
            file_lines[block.file_id] = []
            file_order.append(block.file_id)
        file_lines[block.file_id].extend(lines)

    # 页眉页脚识别：同一段短文本在 3 个以上页面重复出现
    page_counts: dict[str, set[tuple[str, int]]] = {}
    for lines in file_lines.values():
        for line in lines:
            normalized, _ = normalize_with_map(line.text)
            if len(normalized) <= 60:
                page_counts.setdefault(normalized, set()).add(
                    (line.block.file_id, line.block.page_index)
                )

    units: list[tuple[list[ChunkSegment], str, str, str]] = []
    for file_id in file_order:
        chapter = ""
        section = ""
        content_type = "general"
        kept: list[_Line] = []
        for line in file_lines[file_id]:
            normalized, _ = normalize_with_map(line.text)
            if (
                len(normalized) <= 60
                and len(page_counts.get(normalized, set())) >= 3
            ):
                continue  # 页眉/页脚
            kept.append(line)

        # 逐行识别标题，并把正文行归并成段落
        buffer: list[_Line] = []
        buffer_length = 0

        def flush_buffer() -> None:
            nonlocal buffer, buffer_length
            if not buffer:
                return
            segments = _segments_from_lines(buffer)
            if segments:
                units.append((segments, chapter, section, content_type))
            buffer, buffer_length = [], 0

        for index, line in enumerate(kept):
            next_text = kept[index + 1].text if index + 1 < len(kept) else None
            if line.heading is not None:
                # 行内标题（「一、项目实施方案。正文……」）已在展开时切出来
                level, label = line.heading
                flush_buffer()
                if level == "chapter":
                    chapter, section = label, ""
                else:
                    section = label
                content_type = classify_content(chapter, section)
                continue
            heading = _classify_line(
                line.text, next_text, line.block.block_type
            )
            if heading is not None:
                if heading.strip:
                    flush_buffer()
                if heading.level == "chapter":
                    chapter, section = heading.label, ""
                else:
                    section = heading.label
                content_type = classify_content(chapter, section)
                continue

            content_type = classify_content(chapter, section)
            starts_paragraph = bool(
                buffer
                and (
                    line.indented
                    or _LIST_START.match(line.text.strip())
                    or buffer[-1].indented
                )
            )
            if starts_paragraph:
                flush_buffer()
            buffer.append(line)
            buffer_length += len(normalize_with_map(line.text)[0])
            if buffer_length >= target_max:
                flush_buffer()

        flush_buffer()

    # 段落归并成文本块：同一章节、同一内容类型、同一文件的段落可以合并到一个块，
    # 小节只是块的元数据，不作为块的边界，避免每遇到一个小标题就切出一个短块。
    chunks: list[TextChunk] = []
    buffer: list[ChunkSegment] = []
    buffer_sections: list[str] = []
    buffer_key: tuple[str, str, str] | None = None

    def flush() -> None:
        nonlocal buffer, buffer_key, buffer_sections
        if not buffer:
            return
        chapter_value, type_value, _ = buffer_key  # type: ignore[misc]
        section_value = "；".join(part for part in buffer_sections if part)
        length = sum(len(segment.normalized) for segment in buffer)
        if length < min_chars and chunks:
            previous = chunks[-1]
            previous_length = len(previous.normalized)
            # 短块优先与同章节、同内容类型的前一块合并；允许跨小节，但不跨章节
            if (
                previous.chapter == chapter_value
                and previous.content_type == type_value
                and previous.file_id == buffer[0].block.file_id
                and previous_length + length <= max_chars
            ):
                previous.segments.extend(buffer)
                previous.normalized += "".join(segment.normalized for segment in buffer)
                previous.text += "\n" + "\n".join(segment.content for segment in buffer)
                for part in buffer_sections:
                    if part and part not in previous.section.split("；"):
                        previous.section = "；".join(
                            item for item in (previous.section, part) if item
                        )
                buffer, buffer_key, buffer_sections = [], None, []
                return
        chunks.append(_new_chunk(buffer, chapter_value, section_value, type_value, len(chunks)))
        buffer, buffer_key, buffer_sections = [], None, []

    for segments, chapter_value, section_value, type_value in units:
        key = (chapter_value, type_value, segments[0].block.file_id)
        if buffer_key is not None and key != buffer_key:
            flush()
        buffer_key = key
        current_length = sum(len(item.normalized) for item in buffer)
        next_length = sum(len(item.normalized) for item in segments)
        if buffer and current_length + next_length > target_max:
            if not (current_length < target_min and current_length + next_length <= max_chars):
                flush()
                buffer_key = key
        buffer.extend(segments)
        if section_value and section_value not in buffer_sections:
            buffer_sections.append(section_value)

    flush()
    return chunks


def _segments_from_lines(lines: list[_Line]) -> list[ChunkSegment]:
    """把连续的若干行合并成按 block 分组的原文片段。"""
    segments: list[ChunkSegment] = []
    current_block: BlockRef | None = None
    start = end = 0
    for line in lines:
        if line.block is not current_block:
            if current_block is not None:
                segments.append(_make_segment(current_block, start, end))
            current_block = line.block
            start = line.start
            end = line.end
        else:
            end = line.end
    if current_block is not None:
        segments.append(_make_segment(current_block, start, end))
    return segments
