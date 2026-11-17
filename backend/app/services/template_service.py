"""经验模板库：把审核人「这段是行业范本」的判断沉淀为可复用的基准.

批次共同模板（``analysis/template.py``）只看「本次这一批里几家都有」；
本服务让判断跨任务复用：

- 审核「排除」时可勾选「记为模板」，学习**命中片段**（不是整块）；
- 之后的任务按任务快照加载生效片段，命中即标记 ``template_source=library``；
- 片段可撤销、可统计命中次数；全局片段需要管理员。

学到的永远只是「这一小段」，并且保留 200 字长度上限——长段逐字雷同是铁证，
不能被模板吞掉。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ComparisonEvidence, TemplateFragment, User
from app.models.base import utcnow
from app.services import audit
from app.utils.text import normalize, shingle_hashes

MIN_FRAGMENT_CHARS = 30
MAX_FRAGMENT_CHARS = 200
DEFAULT_MATCH_RATIO = 0.8


class TemplateError(Exception):
    """模板库相关的业务校验错误。"""


@dataclass
class TemplateLibrary:
    """一次任务内使用的模板匹配器（只读）。"""

    shingle_size: int
    ratio: float
    fragments: dict[str, list[int]] = field(default_factory=dict)
    inverted: dict[int, set[str]] = field(default_factory=dict)

    def candidates(self, text: str) -> set[str]:
        shingles = shingle_hashes(normalize(text), self.shingle_size)
        hits: dict[str, int] = {}
        for shingle in shingles:
            for fragment_id in self.inverted.get(shingle, ()):
                hits[fragment_id] = hits.get(fragment_id, 0) + 1
        return {
            fragment_id
            for fragment_id, count in hits.items()
            if count >= max(1, int(len(self.fragments.get(fragment_id, [])) * self.ratio * 0.8))
        }

    def matches(self, text: str) -> tuple[str, float] | None:
        """返回包含度最高的片段 (fragment_id, ratio)。"""
        if not self.fragments:
            return None
        shingles = shingle_hashes(normalize(text), self.shingle_size)
        if not shingles:
            return None
        best: tuple[str, float] | None = None
        for fragment_id in self.candidates(text):
            fragment = self.fragments.get(fragment_id) or []
            if not fragment:
                continue
            overlap = len(shingles & set(fragment)) / len(fragment)
            if overlap >= self.ratio and (best is None or overlap > best[1]):
                best = (fragment_id, overlap)
        return best


def load_library(
    db: Session, fragment_ids: list[str] | None, *, shingle_size: int, ratio: float = DEFAULT_MATCH_RATIO
) -> TemplateLibrary:
    library = TemplateLibrary(shingle_size=shingle_size, ratio=ratio)
    if not fragment_ids:
        return library
    rows = db.scalars(
        select(TemplateFragment).where(
            TemplateFragment.id.in_(fragment_ids),
            TemplateFragment.revoked_at.is_(None),
        )
    )
    for row in rows:
        hashes = [int(item) for item in (row.shingle_hashes or [])]
        if not hashes:
            continue
        library.fragments[row.id] = hashes
        for item in hashes:
            library.inverted.setdefault(item, set()).add(row.id)
    return library


def learn_from_evidence(
    db: Session,
    evidence: ComparisonEvidence,
    actor: User | None,
    *,
    scope: str = "project",
    shingle_size: int = 3,
    min_informativeness: float = 0.0,
) -> TemplateFragment:
    """把一条证据的命中片段学成模板.

    只学 ``matched_text``（命中文本），不学整块：整块会把同章节的其他内容
    一起吞掉。
    """
    if scope not in {"project", "global"}:
        raise TemplateError("模板作用域只能是 project 或 global")
    data = evidence.evidence_data or {}
    # 优先用规范化全文；旧证据只有 snippet 时退回它（长度校验会挡住过短的）
    text = data.get("matched_normalized") or data.get("matched_text") or data.get("matched_text_b") or ""
    normalized = normalize(text).strip()
    if len(normalized) < MIN_FRAGMENT_CHARS:
        raise TemplateError(
            f"命中片段过短（{len(normalized)} 字），至少 {MIN_FRAGMENT_CHARS} 字才能记为模板"
        )
    if len(normalized) > MAX_FRAGMENT_CHARS:
        raise TemplateError(
            f"命中片段过长（{len(normalized)} 字），超过 {MAX_FRAGMENT_CHARS} 字的雷同应当作为证据保留，不能记为模板"
        )

    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    project_id = evidence.project_id if scope == "project" else None
    existing = db.scalars(
        select(TemplateFragment).where(
            TemplateFragment.scope == scope,
            TemplateFragment.project_id == project_id,
            TemplateFragment.content_hash == content_hash,
            TemplateFragment.revoked_at.is_(None),
            *( [TemplateFragment.shingle_size == shingle_size] ),
        )
    ).first()
    if existing is not None:
        return existing

    fragment = TemplateFragment(
        scope=scope,
        project_id=project_id,
        normalized_text=normalized,
        content_hash=content_hash,
        shingle_size=shingle_size,
        shingle_hashes=sorted(shingle_hashes(normalized, shingle_size)),
        char_count=len(normalized),
        informativeness=float((data.get("informativeness") or 0.0)),
        source_project_id=evidence.project_id,
        source_evidence_id=evidence.id,
        created_by=actor.id if actor else None,
    )
    db.add(fragment)
    db.flush()
    audit.log_audit(
        db,
        "template_fragment.create",
        "template_fragment",
        fragment.id,
        actor,
        {
            "scope": scope,
            "char_count": fragment.char_count,
            "evidence_id": evidence.id,
        },
    )
    return fragment


def revoke_fragment(
    db: Session, fragment: TemplateFragment, actor: User | None, reason: str | None = None
) -> None:
    fragment.revoked_at = utcnow()
    fragment.revoked_by = actor.id if actor else None
    fragment.revoke_reason = reason
    audit.log_audit(
        db,
        "template_fragment.revoke",
        "template_fragment",
        fragment.id,
        actor,
        {"reason": reason},
    )


def bump_hits(db: Session, hits: dict[str, int]) -> None:
    """任务结束时批量累加命中次数（每条证据不单独写）。"""
    if not hits:
        return
    rows = db.scalars(select(TemplateFragment).where(TemplateFragment.id.in_(list(hits))))
    now = utcnow()
    for row in rows:
        row.hit_count = (row.hit_count or 0) + hits[row.id]
        row.last_hit_at = now
