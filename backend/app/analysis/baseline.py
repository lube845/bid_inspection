"""招标文件基准内容排除（方案第 7 章）.

做法不是删除整段文本，而是：

1. 用与投标文件相同的规范化与 Shingle 规则建立招标文件指纹集合；
2. 计算投标文本中每个字符是否被招标文件指纹覆盖，得到连续覆盖区间；
3. 提供 ``baseline_ratio``、``excluded_ranges`` 和 ``baseline_source``，
   让证据既能看到“这是公共内容”，也能回到招标文件原文。

相似度计算时还会把双方共有的招标指纹从交并集中剔除，得到“有效相似度”，
避免一段文字里同时有公共要求和投标人自主描述时整段被错误排除。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.analysis.chunking import TextChunk
from app.analysis.shingling import stable_hash


@dataclass
class TenderBaseline:
    shingle_size: int
    hashes: set[int] = field(default_factory=set)
    chunks: list[TextChunk] = field(default_factory=list)
    # 指纹 -> (招标文件 Chunk, Chunk 内规范化偏移)
    by_hash: dict[int, tuple[TextChunk, int]] = field(default_factory=dict)
    min_run_chars: int = 10

    @classmethod
    def from_chunks(cls, chunks: list[TextChunk], shingle_size: int) -> "TenderBaseline":
        baseline = cls(shingle_size=shingle_size)
        baseline.chunks = chunks
        for chunk in chunks:
            text = chunk.normalized
            if text and len(text) < shingle_size:
                fingerprint = stable_hash(text)
                baseline.hashes.add(fingerprint)
                baseline.by_hash.setdefault(fingerprint, (chunk, 0))
                continue
            for index in range(max(0, len(text) - shingle_size + 1)):
                fingerprint = stable_hash(text[index : index + shingle_size])
                baseline.hashes.add(fingerprint)
                if fingerprint not in baseline.by_hash:
                    baseline.by_hash[fingerprint] = (chunk, index)
        return baseline

    @classmethod
    def from_hashes(cls, hashes: set[int], shingle_size: int) -> "TenderBaseline":
        """兼容只提供指纹集合的调用方（例如单测）。"""
        return cls(shingle_size=shingle_size, hashes=set(hashes))

    def __bool__(self) -> bool:
        return bool(self.hashes)

    def covered_ranges(self, text: str) -> list[tuple[int, int]]:
        """返回被招标文件覆盖的连续字符区间（规范化文本偏移）。"""
        size = self.shingle_size
        if not text or not self.hashes:
            return []
        if len(text) < size:
            return [(0, len(text))] if stable_hash(text) in self.hashes else []

        covered = bytearray(len(text))
        for index in range(len(text) - size + 1):
            if stable_hash(text[index : index + size]) in self.hashes:
                covered[index : index + size] = b"\x01" * size

        ranges: list[tuple[int, int]] = []
        start = None
        for index, flag in enumerate(covered):
            if flag and start is None:
                start = index
            elif not flag and start is not None:
                if index - start >= self.min_run_chars:
                    ranges.append((start, index))
                start = None
        if start is not None and len(covered) - start >= self.min_run_chars:
            ranges.append((start, len(covered)))
        return ranges

    def coverage_ratio(self, text: str) -> float:
        if not text:
            return 0.0
        size = self.shingle_size
        if len(text) < size:
            return 1.0 if stable_hash(text) in self.hashes else 0.0
        covered = bytearray(len(text))
        for index in range(len(text) - size + 1):
            if stable_hash(text[index : index + size]) in self.hashes:
                covered[index : index + size] = b"\x01" * size
        return sum(covered) / len(text)

    def source_for(self, text: str) -> dict | None:
        """返回一段文字最可能对应的招标文件原文位置。"""
        size = self.shingle_size
        if not text or not self.by_hash:
            return None
        for index in range(min(len(text) - size + 1, 5)):
            entry = self.by_hash.get(stable_hash(text[index : index + size]))
            if entry is not None:
                chunk, offset = entry
                return chunk.source(offset, offset + size)
        return None
