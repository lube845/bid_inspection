"""经验模板库：学习门槛、作用域、匹配与命中统计。"""

from __future__ import annotations

import pytest

from app.models import ComparisonEvidence, Project, TemplateFragment, User
from app.models.enums import UserRole
from app.services import template_service
from app.services.template_service import TemplateError

_LONG_TEXT = (
    "我方将严格按照招标文件要求组织实施本项目，建立项目经理负责制，"
    "明确各岗位职责，实行进度质量安全成本全过程管理，确保按期保质完成。"
)


def _evidence(db, project, text: str = _LONG_TEXT) -> ComparisonEvidence:
    evidence = ComparisonEvidence(
        project_id=project.id,
        analysis_task_id="t1",
        evidence_type="text",
        evidence_subtype="text_similarity",
        severity="medium",
        summary="测试证据",
        evidence_data={"matched_text": text},
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def test_learn_fragment_and_match(db):
    project = Project(project_name="模板项目")
    db.add(project)
    db.commit()
    evidence = _evidence(db, project)

    fragment = template_service.learn_from_evidence(db, evidence, None, shingle_size=3)
    db.commit()
    assert fragment.scope == "project"
    assert fragment.char_count >= template_service.MIN_FRAGMENT_CHARS

    library = template_service.load_library(
        db, [fragment.id], shingle_size=3
    )
    hit = library.matches(_LONG_TEXT)
    assert hit is not None
    assert hit[0] == fragment.id


def test_learning_is_idempotent(db):
    project = Project(project_name="模板项目")
    db.add(project)
    db.commit()
    evidence = _evidence(db, project)

    first = template_service.learn_from_evidence(db, evidence, None)
    db.commit()
    second = template_service.learn_from_evidence(db, evidence, None)
    db.commit()

    assert first.id == second.id
    assert db.query(TemplateFragment).count() == 1


def test_short_fragment_is_rejected(db):
    project = Project(project_name="模板项目")
    db.add(project)
    db.commit()
    evidence = _evidence(db, project, text="短短一句话")

    with pytest.raises(TemplateError):
        template_service.learn_from_evidence(db, evidence, None)


def test_long_fragment_is_rejected(db):
    project = Project(project_name="模板项目")
    db.add(project)
    db.commit()
    evidence = _evidence(db, project, text="长段雷同" * 60)

    with pytest.raises(TemplateError):
        template_service.learn_from_evidence(db, evidence, None)


def test_global_scope_requires_admin_at_route_level_but_service_allows(db):
    project = Project(project_name="模板项目")
    db.add(project)
    db.commit()
    evidence = _evidence(db, project)

    fragment = template_service.learn_from_evidence(db, evidence, None, scope="global")
    db.commit()
    assert fragment.scope == "global"
    assert fragment.project_id is None


def test_revoked_fragment_is_not_loaded(db):
    project = Project(project_name="模板项目")
    db.add(project)
    db.commit()
    evidence = _evidence(db, project)

    fragment = template_service.learn_from_evidence(db, evidence, None)
    db.commit()
    template_service.revoke_fragment(db, fragment, None, "误判")
    db.commit()

    library = template_service.load_library(db, [fragment.id], shingle_size=3)
    assert library.matches(_LONG_TEXT) is None


def test_bump_hits(db):
    project = Project(project_name="模板项目")
    db.add(project)
    db.commit()
    evidence = _evidence(db, project)
    fragment = template_service.learn_from_evidence(db, evidence, None)
    db.commit()

    template_service.bump_hits(db, {fragment.id: 3})
    db.commit()

    refreshed = db.get(TemplateFragment, fragment.id)
    assert refreshed.hit_count == 3
    assert refreshed.last_hit_at is not None


def test_library_snapshot_is_frozen_on_task(db):
    """任务快照只包含启动时生效的片段。"""
    from app.models import AnalysisTask
    from app.models.enums import TaskStatus
    from app.services import analysis_service

    project = Project(project_name="模板项目")
    db.add(project)
    db.commit()
    evidence = _evidence(db, project, _LONG_TEXT)
    fragment = template_service.learn_from_evidence(db, evidence, None)
    db.commit()

    snapshot = analysis_service._build_template_snapshot(db, project.id)
    assert fragment.id in snapshot["fragment_ids"]

    template_service.revoke_fragment(db, fragment, None)
    db.commit()

    task = AnalysisTask(
        project_id=project.id,
        task_no=1,
        status=TaskStatus.COMPLETED.value,
        analysis_config={},
        company_snapshot={"companies": []},
        algorithm_version="test",
        template_snapshot=snapshot,
    )
    db.add(task)
    db.commit()

    # 快照里的 id 仍存在，但已撤销的片段在加载时被过滤
    library = analysis_service._load_template_library(db, task, {})
    assert library.matches(_LONG_TEXT) is None
