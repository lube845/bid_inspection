"""证据聚类：把同一段落的多处命中合成一条「线索」.

现状：一条跨页的相似段落会被切成多条证据（每对文本块一条），审核人要逐条
判断、逐条写结论；20 家企业 190 个组合时这是主要的人力成本。

聚类在后处理阶段进行，不改动检测结果本身，只给证据加上 ``cluster_id`` 等列：

- **同一块对**（chunk_a_id + chunk_b_id 相同）→ 必然合并；
- **相邻续接**（两侧块序号各差 1、章节相同、边界间隙很小）→ 合并。

`cluster_id` 由「组合 id + 字典序最小的块对」派生，因此企业组合重跑后仍然
可复现；只在同一企业组合内聚类，不跨组合——跨组合会让「按组合审核」的语义
失效。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ComparisonEvidence, ComparisonPair
from app.models.enums import EvidenceType, RiskLevel

# 相邻续接的容忍度：两侧块序号各差 1，且边界间隙不超过该字数
_ADJACENT_GAP_CHARS = 60
# 单个线索的成员上限，避免把整章不同段落串成一条
_MAX_CLUSTER_SIZE = 8
_MAX_CLUSTER_GROUPS = 4

_RISK_ORDER = {
    RiskLevel.NONE.value: 0,
    RiskLevel.LOW.value: 1,
    RiskLevel.MEDIUM.value: 2,
    RiskLevel.HIGH.value: 3,
}


@dataclass
class _Node:
    evidence_id: str
    chunk_a: str
    chunk_b: str
    index_a: int
    index_b: int
    a_start: int
    a_end: int
    b_start: int
    b_end: int
    len_a: int
    len_b: int
    file_a: str
    file_b: str
    chapter_key: str
    matched: int
    severity: str


def _node(evidence: ComparisonEvidence) -> _Node | None:
    data = evidence.evidence_data or {}
    chunk_a = data.get("chunk_a_id")
    chunk_b = data.get("chunk_b_id")
    if not chunk_a or not chunk_b:
        return None
    try:
        return _Node(
            evidence_id=evidence.id,
            chunk_a=str(chunk_a),
            chunk_b=str(chunk_b),
            index_a=int(data.get("chunk_index_a") or 0),
            index_b=int(data.get("chunk_index_b") or 0),
            a_start=int(data.get("a_start") or 0),
            a_end=int(data.get("a_end") or 0),
            b_start=int(data.get("b_start") or 0),
            b_end=int(data.get("b_end") or 0),
            len_a=int(data.get("chunk_len_a") or 0),
            len_b=int(data.get("chunk_len_b") or 0),
            file_a=str(data.get("file_a_id") or ""),
            file_b=str(data.get("file_b_id") or ""),
            chapter_key="|".join(
                [
                    str(data.get("chapter_a") or ""),
                    str(data.get("section_a") or ""),
                    str(data.get("chapter_b") or ""),
                    str(data.get("section_b") or ""),
                    str(data.get("content_type_a") or ""),
                    str(data.get("content_type_b") or ""),
                ]
            ),
            matched=int(data.get("matched_chars") or 0),
            severity=evidence.severity,
        )
    except (TypeError, ValueError):
        return None


def _adjacent(a: _Node, b: _Node) -> bool:
    """两个块对是否构成「同一段落的相邻续接」。"""
    if a.file_a != b.file_a or a.file_b != b.file_b:
        return False
    if a.chapter_key != b.chapter_key:
        return False

    delta_a = b.index_a - a.index_a
    delta_b = b.index_b - a.index_b
    if (delta_a, delta_b) not in {(1, 0), (0, 1), (1, 1)}:
        return False

    # 前一块的命中应结束在块尾、后一块的命中应开始于块首，否则是两块里
    # 各自无关的片段。
    if delta_a == 1 and (a.len_a - a.a_end > _ADJACENT_GAP_CHARS or b.a_start > _ADJACENT_GAP_CHARS):
        return False
    if delta_b == 1 and (a.len_b - a.b_end > _ADJACENT_GAP_CHARS or b.b_start > _ADJACENT_GAP_CHARS):
        return False
    return True


def build_clusters(evidences: list[ComparisonEvidence]) -> list[list[ComparisonEvidence]]:
    """对单个企业组合的文本证据做聚类，返回线索列表。"""
    nodes = {evidence.id: _node(evidence) for evidence in evidences}
    by_id = {evidence.id: evidence for evidence in evidences}
    ordered = sorted(
        (node for node in nodes.values() if node is not None),
        key=lambda node: (node.index_a, node.a_start, node.index_b, node.b_start),
    )

    parent = {node.evidence_id: node.evidence_id for node in ordered}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    # 1) 同一块对必定合并
    seen_pairs: dict[tuple[str, str], str] = {}
    for node in ordered:
        key = (node.chunk_a, node.chunk_b)
        if key in seen_pairs:
            union(seen_pairs[key], node.evidence_id)
        else:
            seen_pairs[key] = node.evidence_id

    # 2) 相邻续接
    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            if find(left.evidence_id) == find(right.evidence_id):
                continue
            if _adjacent(left, right):
                union(left.evidence_id, right.evidence_id)

    groups: dict[str, list[ComparisonEvidence]] = {}
    for node in ordered:
        groups.setdefault(find(node.evidence_id), []).append(by_id[node.evidence_id])

    # 约束单簇规模：超出上限的按块对数量拆开（保留最靠后的成员成新簇）
    result: list[list[ComparisonEvidence]] = []
    for members in groups.values():
        if len(members) <= _MAX_CLUSTER_SIZE:
            result.append(members)
            continue
        chunk_pairs: list[str] = []
        for member in members:
            data = member.evidence_data or {}
            key = f"{data.get('chunk_a_id')}|{data.get('chunk_b_id')}"
            if key not in chunk_pairs:
                chunk_pairs.append(key)
        if len(chunk_pairs) <= _MAX_CLUSTER_GROUPS:
            result.append(members)
            continue
        keep = set(chunk_pairs[:_MAX_CLUSTER_GROUPS])
        kept, rest = [], []
        for member in members:
            data = member.evidence_data or {}
            key = f"{data.get('chunk_a_id')}|{data.get('chunk_b_id')}"
            (kept if key in keep else rest).append(member)
        result.append(kept)
        result.extend([member] for member in rest)
    return [group for group in result if group]


def _cluster_id(pair_id: str, members: list[ComparisonEvidence]) -> str:
    keys = sorted(
        f"{member.evidence_data.get('chunk_a_id')}|{member.evidence_data.get('chunk_b_id')}"
        for member in members
        if member.evidence_data
    )
    digest = hashlib.sha1(f"{pair_id}|{keys[0] if keys else ''}".encode("utf-8")).hexdigest()
    return digest[:32]


def assign_clusters(
    db: Session, task_id: str, pair_ids: list[str] | None = None
) -> dict[str, int]:
    """给文本证据写入聚类字段，返回 {cluster_id: 成员数}。"""
    query = select(ComparisonPair).where(ComparisonPair.analysis_task_id == task_id)
    if pair_ids:
        query = query.where(ComparisonPair.id.in_(pair_ids))
    pairs = list(db.scalars(query))

    summary: dict[str, int] = {}
    for pair in pairs:
        evidences = list(
            db.scalars(
                select(ComparisonEvidence).where(
                    ComparisonEvidence.comparison_pair_id == pair.id,
                    ComparisonEvidence.evidence_type == EvidenceType.TEXT.value,
                    ComparisonEvidence.is_current.is_(True),
                )
            )
        )
        # 先清零，保证重跑后旧聚类不残留
        for evidence in evidences:
            evidence.cluster_id = None
            evidence.cluster_size = 1
            evidence.cluster_index = 0
            evidence.cluster_lead_id = None
            evidence.cluster_rule = ""

        for group in build_clusters(evidences):
            cid = _cluster_id(pair.id, group)
            ordered = sorted(
                group,
                key=lambda item: (
                    _RISK_ORDER.get(item.severity, 0),
                    int((item.evidence_data or {}).get("matched_chars") or 0),
                    -int((item.evidence_data or {}).get("a_start") or 0),
                ),
                reverse=True,
            )
            lead = ordered[0]
            size = len(group)
            summary[cid] = size
            for index, evidence in enumerate(group, start=1):
                evidence.cluster_id = cid
                evidence.cluster_size = size
                evidence.cluster_index = index
                evidence.cluster_lead_id = lead.id
                evidence.cluster_rule = (
                    "exact_chunk_pair" if size == 1 else "adjacent_continuation"
                )
    db.flush()
    return summary
