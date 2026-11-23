"""模板命中统计的端到端回归.

此前 ``_run_pair`` 直接引用 ``run_task`` 的局部变量 ``template_hits``：只要本任务
用到的模板库真的命中了某段雷同，就会抛 ``NameError``。异常被「单个企业组合失败
不影响其他组合」的兜底捕获，于是表现为**整组静默失败**——证据全空、组合状态
failed，而任务仍可能被判为完成。既有测试都没有构造「模板库存在且命中」的整任务，
因此一直没暴露。

构造方式：复用种子数据与真实文本引擎，取一对企业组合里最短的一条（<200 字，
不超过长段豁免阈值）命中文本做成模板片段，写进任务快照后重跑任务。命中的组合
必须仍然是 completed，且片段命中次数被累加。
"""

import hashlib

from sqlalchemy import select

from app.analysis.text_similarity import match_chunks
from app.models import AnalysisTask, ComparisonPair, TemplateFragment
from app.models.enums import TaskStatus
from app.services import analysis_service
from app.utils.text import normalize, shingle_hashes

_SHINGLE_SIZE = 3
# 与 text_thresholds 的 template_max_chars_guard 一致：更长的命中不会被模板豁免
_MAX_CHARS_GUARD = 200


def _build_fragment_from_real_match(db, task: AnalysisTask) -> TemplateFragment:
    """在一块真实命中文本上建模板片段，保证跑任务时一定能命中。"""
    snapshot = {c["company_id"]: c for c in task.company_snapshot["companies"]}
    blocks = {
        company_id: analysis_service._blocks_for_snapshot(db, company)
        for company_id, company in snapshot.items()
    }
    thresholds = task.analysis_config.get("thresholds") or {}
    _, chunks, index, tender, _ = analysis_service._prepare_text_engine(
        db, task, blocks, thresholds
    )
    company_ids = list(snapshot)
    matches, _ = match_chunks(
        index.candidates_between(chunks[company_ids[0]], chunks[company_ids[1]]),
        tender,
        thresholds=thresholds,
    )
    short = [m for m in matches if 0 < m.matched_chars < _MAX_CHARS_GUARD]
    assert short, "种子数据里应至少有一条短于 200 字的命中可供构造模板"
    text = normalize(short[0].text).strip()
    assert len(text) >= 30, "模板片段至少要 30 字"

    fragment = TemplateFragment(
        scope="project",
        project_id=task.project_id,
        normalized_text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        shingle_size=_SHINGLE_SIZE,
        shingle_hashes=sorted(shingle_hashes(text, _SHINGLE_SIZE)),
        char_count=len(text),
        informativeness=0.0,
        source_project_id=task.project_id,
    )
    db.add(fragment)
    db.commit()
    return fragment


def _use_fragment(db, task: AnalysisTask, fragment: TemplateFragment) -> None:
    """把片段加入任务快照，模拟「任务启动时该模板已生效」。"""
    snapshot = dict(task.template_snapshot or {})
    snapshot["fragment_ids"] = [fragment.id]
    task.template_snapshot = snapshot
    db.commit()


def test_template_hits_do_not_break_pairs(db):
    from app.seed import seed_demo

    seed_demo(db)
    task = db.scalars(select(AnalysisTask)).first()
    fragment = _build_fragment_from_real_match(db, task)
    _use_fragment(db, task, fragment)
    # run_task 对已完成任务直接返回；这里要验证的是「重跑整任务」，
    # 因此先把状态改回排队，重跑清理逻辑在 run_task 内部。
    task.status = TaskStatus.QUEUED.value
    db.commit()

    analysis_service.run_task(db, task.id)
    db.refresh(task)

    pairs = list(
        db.scalars(select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id))
    )
    assert pairs
    # 关键断言：模板命中不得让任何企业组合静默失败
    assert all(pair.status == "completed" for pair in pairs), [
        (pair.id, pair.status, pair.error_message) for pair in pairs
    ]
    assert all(pair.error_message is None for pair in pairs)

    db.refresh(fragment)
    assert (fragment.hit_count or 0) > 0, "命中片段应累加使用次数"


def test_retry_pair_accumulates_template_hits(db):
    from app.seed import seed_demo

    seed_demo(db)
    task = db.scalars(select(AnalysisTask)).first()
    fragment = _build_fragment_from_real_match(db, task)
    _use_fragment(db, task, fragment)
    pair = db.scalars(
        select(ComparisonPair).where(ComparisonPair.analysis_task_id == task.id)
    ).first()

    analysis_service.retry_pair(db, task, pair)
    db.refresh(pair)
    db.refresh(fragment)

    assert pair.status == "completed"
    assert pair.error_message is None
    assert (fragment.hit_count or 0) > 0
