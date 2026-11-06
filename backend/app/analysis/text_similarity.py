"""文本相似度检测（方案第 8～10 章）.

完整链路：

    结构化分块 → MinHash 签名 → LSH 候选召回 → Jaccard 精确计算
    → 招标文件基准排除 → 局部文本对齐 → 证据生成

关键约束：

- LSH 只负责“召回”，不能把 LSH 命中当作异常；
- Jaccard 在完整 Shingle 集合上计算，同时计算剔除招标文件指纹后的
  ``effective_jaccard``，风险判定优先使用有效相似度；
- 局部对齐使用 SequenceMatcher 找连续相似片段，并把偏移量映射回原文；
- 所有参数来自分析任务启动时的规则快照；
- 算法版本、规范化版本、Shingle 参数随证据一起保存，历史结果可复现。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

import numpy as np

from app.analysis.base import EvidenceDraft
from app.analysis.baseline import TenderBaseline
from app.analysis.chunking import TextChunk, build_chunks
from app.analysis.shingling import (
    MinHashLSH,
    MinHasher,
    optimal_bands,
    shingle_set,
)
from app.analysis.sources import BlockRef
from app.analysis.thresholds import text_thresholds
from app.core.config import settings
from app.models.enums import EvidenceType, RiskLevel
from app.utils.text import stable_hash

# 局部对齐：允许的编辑间隔（规范化字符数）与局部密度下限
_GAP_TOLERANCE = 30
_MIN_LOCAL_RATIO = 0.65
# 局部片段所在块对的最低包含度：避免几十个通用字的零散重合形成证据
_MIN_BLOCK_CONTAINMENT = 0.2
_MIN_LONG_MATCH_CHARS = 100

ALGORITHM_NAME = "minhash_lsh+jaccard+sequence_matcher"
NORMALIZATION_VERSION = "norm-v2"


@dataclass
class TextMatch:
    chunk_a: TextChunk
    chunk_b: TextChunk
    a_start: int
    a_end: int
    b_start: int
    b_end: int
    matched_chars: int
    similarity: float
    effective_similarity: float
    containment: float
    effective_containment: float
    local_ratio: float
    intersection_size: int
    union_size: int
    effective_intersection_size: int
    effective_union_size: int
    baseline_ratio: float
    excluded_by_tender: bool
    text: str
    localization_status: str = "local"


@dataclass
class SimilarityStats:
    jaccard: float
    effective_jaccard: float
    containment: float
    effective_containment: float
    intersection_size: int
    union_size: int
    effective_intersection_size: int
    effective_union_size: int
    baseline_ratio: float


def _stats(set_a: set[int], set_b: set[int], tender: set[int]) -> SimilarityStats:
    intersection = set_a & set_b
    union = set_a | set_b
    effective_intersection = intersection - tender
    effective_union = union - tender
    min_size = min(len(set_a), len(set_b))
    effective_min_size = min(len(set_a - tender), len(set_b - tender))
    return SimilarityStats(
        jaccard=len(intersection) / len(union) if union else 0.0,
        effective_jaccard=(
            len(effective_intersection) / len(effective_union) if effective_union else 0.0
        ),
        containment=len(intersection) / min_size if min_size else 0.0,
        effective_containment=(
            len(effective_intersection) / effective_min_size if effective_min_size else 0.0
        ),
        intersection_size=len(intersection),
        union_size=len(union),
        effective_intersection_size=len(effective_intersection),
        effective_union_size=len(effective_union),
        baseline_ratio=len(union & tender) / len(union) if union else 0.0,
    )


class TextIndex:
    """项目级文本索引：同一任务的所有投标人 Chunk 进入统一候选检索范围。

    索引键含算法参数版本（由 MinHashLSH 实例承载），不同配置的签名不会混用。
    只保存签名和 Chunk 引用，Shingle 集合在比较时按需计算，控制内存占用。
    """

    def __init__(self, rules: dict):
        self.rules = rules
        self.shingle_size = int(rules["text_shingle_size"])
        self.num_perm = int(rules["text_minhash_num_perm"])
        self.threshold = float(rules["text_lsh_threshold"])
        self.hasher = MinHasher(self.num_perm)
        self.lsh = MinHashLSH(self.num_perm, self.threshold)
        self.chunks: dict[str, TextChunk] = {}
        self.signatures: dict[str, np.ndarray] = {}

    def add_chunks(self, chunks: list[TextChunk]) -> None:
        for chunk in chunks:
            if not chunk.normalized:
                continue
            signature = self.hasher.signature(
                shingle_set(chunk.normalized, self.shingle_size)
            )
            self.chunks[chunk.chunk_id] = chunk
            self.signatures[chunk.chunk_id] = signature
            self.lsh.add(chunk.chunk_id, signature)

    @property
    def lsh_params(self) -> dict:
        return {
            "bands": self.lsh.params.bands,
            "rows": self.lsh.params.rows,
            "threshold": self.threshold,
            "num_perm": self.num_perm,
        }

    def candidates_between(
        self,
        chunks_a: list[TextChunk],
        chunks_b: list[TextChunk],
    ) -> list[tuple[TextChunk, TextChunk, int]]:
        """A、B 两家投标人之间的候选 Chunk 对，按 LSH 命中 band 数排序。"""
        target_ids = {chunk.chunk_id for chunk in chunks_b}
        if not target_ids:
            return []
        votes_by_pair: dict[tuple[str, str], int] = {}
        for chunk in chunks_a:
            signature = self.signatures.get(chunk.chunk_id)
            if signature is None:
                continue
            for candidate_id, votes in self.lsh.query(signature, exclude=chunk.chunk_id).items():
                if candidate_id not in target_ids:
                    continue
                key = (chunk.chunk_id, candidate_id)
                if votes > votes_by_pair.get(key, 0):
                    votes_by_pair[key] = votes

        ordered = sorted(votes_by_pair.items(), key=lambda item: item[1], reverse=True)
        limit = int(self.rules["max_candidate_pairs"])
        if len(ordered) > limit:
            ordered = ordered[:limit]
        return [
            (self.chunks[key[0]], self.chunks[key[1]], votes)
            for key, votes in ordered
        ]


def _align_chunks(
    chunk_a: TextChunk, chunk_b: TextChunk, rules: dict
) -> list[tuple[int, int, int, int, int, float]]:
    """返回 (a_start, a_end, b_start, b_end, matched_chars, local_ratio)。"""
    na, nb = chunk_a.normalized, chunk_b.normalized
    if not na or not nb:
        return []
    matcher = SequenceMatcher(None, na, nb, autojunk=False)
    blocks = [match for match in matcher.get_matching_blocks() if match.size > 0]
    if not blocks:
        return []

    groups: list[list] = []
    for match in blocks:
        if groups:
            group = groups[-1]
            last = group[-1]
            last_a_end = last.a + last.size
            last_b_end = last.b + last.size
            gap_ok = (
                match.a - last_a_end <= _GAP_TOLERANCE
                and match.b - last_b_end <= _GAP_TOLERANCE
            )
            if gap_ok:
                # 只有合并后仍保持足够局部密度时才合并，避免把零散的单字命中
                # 串成一个跨越整段文本的“伪相似片段”
                first = group[0]
                new_a_end = match.a + match.size
                new_b_end = match.b + match.size
                new_span = max(new_a_end - first.a, new_b_end - first.b)
                new_matched = sum(item.size for item in group) + match.size
                if new_span and new_matched / new_span >= _MIN_LOCAL_RATIO:
                    group.append(match)
                    continue
        groups.append([match])

    minimum = int(rules["text_min_match_chars"])
    results: list[tuple[int, int, int, int, int, float]] = []
    for group in groups:
        first, last = group[0], group[-1]
        a_start, a_end = first.a, last.a + last.size
        b_start, b_end = first.b, last.b + last.size
        matched = sum(match.size for match in group)
        if matched < minimum:
            continue
        span = max(a_end - a_start, b_end - b_start)
        local_ratio = matched / span if span else 0.0
        if local_ratio < _MIN_LOCAL_RATIO:
            continue
        results.append((a_start, a_end, b_start, b_end, matched, local_ratio))
    return results


def match_chunks(
    candidates: list[tuple[TextChunk, TextChunk, int]],
    tender: TenderBaseline | None = None,
    *,
    thresholds: dict | None = None,
) -> tuple[list[TextMatch], dict]:
    """对 LSH 候选做 Jaccard 精确计算和局部对齐。"""
    rules = text_thresholds(thresholds)
    shingle_size = int(rules["text_shingle_size"])
    minimum_block = int(rules["text_min_block_chars"])
    similarity_floor = float(rules["text_similarity"])
    tender_hashes = tender.hashes if tender else set()

    shingle_cache: dict[int, set[int]] = {}
    matches: list[TextMatch] = []
    stats = {
        "candidate_count": len(candidates),
        "local_match_count": 0,
        "result_count": 0,
        "block_level_count": 0,
    }

    for chunk_a, chunk_b, _votes in candidates:
        if len(chunk_a.normalized) < minimum_block or len(chunk_b.normalized) < minimum_block:
            continue
        # 用对象身份做缓存键：即使调用方传入的 chunk_id 重复，也不能串用 Shingle 集合
        set_a = shingle_cache.get(id(chunk_a))
        if set_a is None:
            set_a = shingle_set(chunk_a.normalized, shingle_size)
            shingle_cache[id(chunk_a)] = set_a
        set_b = shingle_cache.get(id(chunk_b))
        if set_b is None:
            set_b = shingle_set(chunk_b.normalized, shingle_size)
            shingle_cache[id(chunk_b)] = set_b

        similarity = _stats(set_a, set_b, tender_hashes)

        local = _align_chunks(chunk_a, chunk_b, rules)
        if local:
            for a_start, a_end, b_start, b_end, matched, local_ratio in local:
                if (
                    similarity.jaccard < _MIN_BLOCK_CONTAINMENT
                    and similarity.containment < _MIN_BLOCK_CONTAINMENT
                    and matched < _MIN_LONG_MATCH_CHARS
                ):
                    continue
                stats["local_match_count"] += 1
                text = chunk_a.normalized[a_start:a_end]
                baseline_ratio = tender.coverage_ratio(text) if tender else 0.0
                matches.append(
                    TextMatch(
                        chunk_a=chunk_a,
                        chunk_b=chunk_b,
                        a_start=a_start,
                        a_end=a_end,
                        b_start=b_start,
                        b_end=b_end,
                        matched_chars=matched,
                        similarity=round(similarity.jaccard, 4),
                        effective_similarity=round(similarity.effective_jaccard, 4),
                        containment=round(similarity.containment, 4),
                        effective_containment=round(similarity.effective_containment, 4),
                        local_ratio=round(local_ratio, 4),
                        intersection_size=similarity.intersection_size,
                        union_size=similarity.union_size,
                        effective_intersection_size=similarity.effective_intersection_size,
                        effective_union_size=similarity.effective_union_size,
                        baseline_ratio=round(baseline_ratio, 4),
                        excluded_by_tender=(
                            baseline_ratio >= float(rules["tender_exclusion_ratio"])
                        ),
                        text=text,
                    )
                )
            continue

        # 局部对齐没有可用片段，但块级相似度足够高：保留块级结果并标记定位方式
        full_pass = (
            similarity.jaccard >= similarity_floor
            or similarity.containment >= 0.8
            or similarity.effective_jaccard >= similarity_floor
            or similarity.effective_containment >= 0.8
        )
        block_matched = min(len(chunk_a.normalized), len(chunk_b.normalized))
        if full_pass and block_matched >= int(rules["text_min_match_chars"]):
            text = chunk_a.normalized
            baseline_ratio = tender.coverage_ratio(text) if tender else 0.0
            stats["block_level_count"] += 1
            matches.append(
                TextMatch(
                    chunk_a=chunk_a,
                    chunk_b=chunk_b,
                    a_start=0,
                    a_end=len(chunk_a.normalized),
                    b_start=0,
                    b_end=len(chunk_b.normalized),
                    matched_chars=block_matched,
                    similarity=round(similarity.jaccard, 4),
                    effective_similarity=round(similarity.effective_jaccard, 4),
                    containment=round(similarity.containment, 4),
                    effective_containment=round(similarity.effective_containment, 4),
                    local_ratio=round(similarity.jaccard, 4),
                    intersection_size=similarity.intersection_size,
                    union_size=similarity.union_size,
                    effective_intersection_size=similarity.effective_intersection_size,
                    effective_union_size=similarity.effective_union_size,
                    baseline_ratio=round(baseline_ratio, 4),
                    excluded_by_tender=(
                        baseline_ratio >= float(rules["tender_exclusion_ratio"])
                    ),
                    text=text,
                    localization_status="block_level",
                )
            )

    matches.sort(key=lambda item: (item.matched_chars, item.local_ratio), reverse=True)
    matches = matches[: int(rules["max_matches_per_pair"])]
    stats["result_count"] = len(matches)
    return matches, stats


def find_text_matches(
    blocks_a: list[BlockRef],
    blocks_b: list[BlockRef],
    tender_shingles: set[int] | TenderBaseline | None = None,
    *,
    thresholds: dict | None = None,
) -> list[TextMatch]:
    """兼容旧调用：直接传入两家企业的文本块列表。

    新流程（analysis_service）会预先构建 Chunk 和项目级 TextIndex，避免每个
    企业组合重复计算签名；这个函数主要服务于单测和独立调用。
    """
    rules = text_thresholds(thresholds)
    chunks_a = build_chunks(blocks_a, rules)
    chunks_b = build_chunks(blocks_b, rules)
    index = TextIndex(rules)
    index.add_chunks(chunks_a + chunks_b)
    tender = _ensure_baseline(tender_shingles, int(rules["text_shingle_size"]))
    candidates = index.candidates_between(chunks_a, chunks_b)
    matches, _ = match_chunks(candidates, tender, thresholds=thresholds)
    return matches


def _ensure_baseline(
    tender: set[int] | TenderBaseline | None, shingle_size: int
) -> TenderBaseline | None:
    if tender is None:
        return None
    if isinstance(tender, TenderBaseline):
        return tender
    return TenderBaseline.from_hashes(tender, shingle_size)


def _severity(matched: int, local_ratio: float) -> str:
    if matched >= 300 and local_ratio >= 0.9:
        return RiskLevel.HIGH.value
    if matched >= 120 or local_ratio >= 0.9:
        return RiskLevel.MEDIUM.value
    return RiskLevel.LOW.value


def build_text_evidences(
    matches: list[TextMatch],
    tender: set[int] | TenderBaseline | None,
    *,
    thresholds: dict | None = None,
) -> list[EvidenceDraft]:
    rules = text_thresholds(thresholds)
    baseline = _ensure_baseline(tender, int(rules["text_shingle_size"]))
    exclusion_ratio = float(rules["tender_exclusion_ratio"])
    num_perm = int(rules["text_minhash_num_perm"])
    lsh_threshold = float(rules["text_lsh_threshold"])
    bands, rows = optimal_bands(num_perm, lsh_threshold)
    drafts: list[EvidenceDraft] = []
    # 同一条线索可能被多个文本块对重复命中（例如同一段文字在文档里出现两次，
    # 或同一段落被切成了不同的块）。这些重复会让证据列表出现 summary 完全相同
    # 的多条记录，并把风险计数抬高，必须按「文件对 + 页码 + 匹配文本」归并。
    seen: dict[tuple, int] = {}

    for match in matches[: int(rules["max_matches_per_pair"])]:
        source_a = match.chunk_a.source(match.a_start, match.a_end)
        source_b = match.chunk_b.source(match.b_start, match.b_end)

        dedup_key = (
            match.chunk_a.company_id,
            match.chunk_b.company_id,
            match.chunk_a.file_id,
            match.chunk_b.file_id,
            source_a.get("page_index"),
            source_b.get("page_index"),
            stable_hash(match.text),
            stable_hash(match.chunk_b.normalized[match.b_start : match.b_end]),
        )
        existing = seen.get(dedup_key)
        if existing is not None:
            drafts[existing].evidence_data["merged_duplicate_count"] = (
                drafts[existing].evidence_data.get("merged_duplicate_count", 0) + 1
            )
            continue

        local_text = match.text
        local_text_b = match.chunk_b.normalized[match.b_start : match.b_end]
        if baseline is not None:
            coverage_a = baseline.coverage_ratio(local_text)
            coverage_b = baseline.coverage_ratio(local_text_b)
            local_coverage = max(coverage_a, coverage_b)
        else:
            local_coverage = match.baseline_ratio
        excluded = local_coverage >= exclusion_ratio
        excluded_ranges_a: list[dict] = []
        excluded_ranges_b: list[dict] = []
        baseline_source = None
        if baseline is not None and local_coverage > 0:
            baseline_source = baseline.source_for(local_text)
            for start, end in baseline.covered_ranges(local_text):
                excluded_ranges_a.extend(
                    match.chunk_a.original_ranges(match.a_start + start, match.a_start + end)
                )
            for start, end in baseline.covered_ranges(local_text_b):
                excluded_ranges_b.extend(
                    match.chunk_b.original_ranges(match.b_start + start, match.b_start + end)
                )

        effective_chars = int(round(match.matched_chars * (1 - local_coverage)))
        severity = _severity(effective_chars, match.local_ratio)
        if excluded and severity == RiskLevel.HIGH.value:
            severity = RiskLevel.MEDIUM.value

        summary = (
            f"{match.chunk_a.company_name}《{match.chunk_a.file_name}》"
            f"第 {source_a.get('page_index')} 页与 "
            f"{match.chunk_b.company_name}《{match.chunk_b.file_name}》"
            f"第 {source_b.get('page_index')} 页存在 {match.matched_chars} 字相似文本"
        )
        if match.localization_status != "local":
            summary += "（块级命中，未能精确定位局部片段）"

        draft = EvidenceDraft(
                evidence_type=EvidenceType.TEXT.value,
                subtype="text_similarity",
                severity=severity,
                summary=summary,
                evidence_data={
                    # 块级相似度
                    "similarity": match.similarity,
                    "similarity_percent": round(match.similarity * 100, 1),
                    "effective_similarity": match.effective_similarity,
                    "containment": match.containment,
                    "effective_containment": match.effective_containment,
                    "intersection_size": match.intersection_size,
                    "union_size": match.union_size,
                    "effective_intersection_size": match.effective_intersection_size,
                    "effective_union_size": match.effective_union_size,
                    # 局部片段
                    "local_ratio": match.local_ratio,
                    "local_similarity_percent": round(match.local_ratio * 100, 1),
                    "matched_chars": match.matched_chars,
                    "evidence_length": match.a_end - match.a_start,
                    "matched_text": source_a.get("snippet"),
                    "matched_text_b": source_b.get("snippet"),
                    "localization_status": match.localization_status,
                    # 基准排除
                    "baseline_match": bool(local_coverage > 0),
                    "baseline_ratio": round(local_coverage, 4),
                    "block_baseline_ratio": match.baseline_ratio,
                    "baseline_source": baseline_source,
                    "excluded_ranges_a": excluded_ranges_a,
                    "excluded_ranges_b": excluded_ranges_b,
                    "excluded_by_tender": excluded,
                    "exclusion_ratio": round(local_coverage, 4),
                    # 结构定位
                    "chunk_a_id": match.chunk_a.chunk_id,
                    "chunk_b_id": match.chunk_b.chunk_id,
                    "block_a_id": match.chunk_a.first_block.block_id,
                    "block_b_id": match.chunk_b.first_block.block_id,
                    "chapter_a": match.chunk_a.chapter,
                    "chapter_b": match.chunk_b.chapter,
                    "section_a": match.chunk_a.section,
                    "section_b": match.chunk_b.section,
                    "content_type_a": match.chunk_a.content_type,
                    "content_type_b": match.chunk_b.content_type,
                    # 算法可追溯
                    "algorithm": ALGORITHM_NAME,
                    "algorithm_version": settings.algorithm_version,
                    "normalization_version": NORMALIZATION_VERSION,
                    "shingle_size": int(rules["text_shingle_size"]),
                    "minhash_num_perm": num_perm,
                    "lsh_threshold": lsh_threshold,
                    "lsh_bands": bands,
                    "lsh_rows": rows,
                    "merged_duplicate_count": 0,
                },
                source_a=source_a,
                source_b=source_b,
            )
        seen[dedup_key] = len(drafts)
        drafts.append(draft)
    return drafts


def build_tender_baseline(
    chunks: list[TextChunk], *, thresholds: dict | None = None
) -> TenderBaseline:
    rules = text_thresholds(thresholds)
    return TenderBaseline.from_chunks(chunks, int(rules["text_shingle_size"]))
