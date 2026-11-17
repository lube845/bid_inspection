"""证据聚类：同一段落的多处命中合成一条线索。"""

from __future__ import annotations

from app.analysis.clustering import build_clusters
from app.models import ComparisonEvidence


def _evidence(
    evidence_id: str,
    *,
    chunk_a: str,
    chunk_b: str,
    index_a: int = 0,
    index_b: int = 0,
    a_start: int = 0,
    a_end: int = 100,
    b_start: int = 0,
    b_end: int = 100,
    len_a: int = 120,
    len_b: int = 120,
    chapter: str = "第一章",
    matched: int = 80,
    severity: str = "medium",
    file_a: str = "f1",
    file_b: str = "f2",
) -> ComparisonEvidence:
    evidence = ComparisonEvidence(
        project_id="p1",
        analysis_task_id="t1",
        comparison_pair_id="pair1",
        evidence_type="text",
        evidence_subtype="text_duplicate",
        severity=severity,
        summary="",
        evidence_data={
            "chunk_a_id": chunk_a,
            "chunk_b_id": chunk_b,
            "chunk_index_a": index_a,
            "chunk_index_b": index_b,
            "a_start": a_start,
            "a_end": a_end,
            "b_start": b_start,
            "b_end": b_end,
            "chunk_len_a": len_a,
            "chunk_len_b": len_b,
            "chapter_a": chapter,
            "chapter_b": chapter,
            "section_a": "",
            "section_b": "",
            "content_type_a": "technical",
            "content_type_b": "technical",
            "matched_chars": matched,
            "file_a_id": file_a,
            "file_b_id": file_b,
        },
    )
    evidence.id = evidence_id
    return evidence


def test_same_chunk_pair_is_merged():
    items = [
        _evidence("e1", chunk_a="ca1", chunk_b="cb1"),
        _evidence("e2", chunk_a="ca1", chunk_b="cb1", matched=60),
    ]
    groups = build_clusters(items)
    assert len(groups) == 1
    assert {item.id for item in groups[0]} == {"e1", "e2"}


def test_adjacent_continuation_is_merged():
    items = [
        _evidence("e1", chunk_a="ca1", chunk_b="cb1", index_a=0, index_b=0, a_end=120, b_end=120),
        _evidence("e2", chunk_a="ca2", chunk_b="cb2", index_a=1, index_b=1, a_start=0, b_start=0, len_a=100, len_b=100, a_end=100, b_end=100),
    ]
    # 第一条命中结束在块尾、第二条从块首开始 → 视为同一段落的续接
    groups = build_clusters(items)
    assert len(groups) == 1


def test_distant_passages_are_not_merged():
    items = [
        _evidence("e1", chunk_a="ca1", chunk_b="cb1", index_a=0, index_b=0, a_start=0, a_end=50),
        _evidence("e2", chunk_a="ca5", chunk_b="cb5", index_a=4, index_b=4, a_start=0, a_end=50),
    ]
    groups = build_clusters(items)
    assert len(groups) == 2


def test_different_chapters_are_not_merged():
    items = [
        _evidence("e1", chunk_a="ca1", chunk_b="cb1", index_a=0, index_b=0, chapter="第一章", a_end=120),
        _evidence("e2", chunk_a="ca2", chunk_b="cb2", index_a=1, index_b=1, chapter="第二章"),
    ]
    groups = build_clusters(items)
    assert len(groups) == 2


def test_large_groups_are_split_by_chunk_pairs():
    items = []
    for index in range(12):
        items.append(
            _evidence(
                f"e{index}",
                chunk_a=f"ca{index}",
                chunk_b=f"cb{index}",
                index_a=index,
                index_b=index,
            )
        )
    groups = build_clusters(items)
    assert all(len(group) <= 8 for group in groups)
    assert sum(len(group) for group in groups) == 12
