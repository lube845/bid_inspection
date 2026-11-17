"""跨任务结果对比。"""

from __future__ import annotations

import pytest

from app.models import (
    AnalysisTask,
    ComparisonEvidence,
    ComparisonPair,
    Project,
)
from app.models.enums import TaskStatus
from app.services import results_service


def _task(db, project, task_no: int, files: list[dict] | None = None) -> AnalysisTask:
    task = AnalysisTask(
        project_id=project.id,
        task_no=task_no,
        status=TaskStatus.COMPLETED.value,
        analysis_config={
            "rule_versions": {"text": {"version_no": task_no}},
            "thresholds": {},
        },
        company_snapshot={
            "companies": [
                {
                    "company_id": "c1",
                    "company_name": "甲公司",
                    "files": files or [
                        {"id": "f1", "file_name": "甲.pdf", "version": 1, "file_hash": "h1", "page_count": 3}
                    ],
                }
            ]
        },
        algorithm_version="test",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _pair(db, task, overall_risk: str = "low") -> ComparisonPair:
    pair = ComparisonPair(
        analysis_task_id=task.id,
        company_a_id="c1",
        company_b_id="c2",
        overall_risk=overall_risk,
    )
    db.add(pair)
    db.commit()
    db.refresh(pair)
    return pair


def _evidence(db, task, pair, *, chunk_a: str, chunk_b: str, severity: str, summary: str, review="pending"):
    evidence = ComparisonEvidence(
        project_id=task.project_id,
        analysis_task_id=task.id,
        comparison_pair_id=pair.id,
        evidence_type="text",
        evidence_subtype="text_similarity",
        severity=severity,
        summary=summary,
        evidence_data={"chunk_a_id": chunk_a, "chunk_b_id": chunk_b},
        review_status=review,
    )
    db.add(evidence)
    db.commit()
    return evidence


@pytest.fixture()
def world(db):
    project = Project(project_name="对比项目")
    db.add(project)
    db.commit()
    db.refresh(project)
    old = _task(db, project, 1)
    new = _task(
        db,
        project,
        2,
        files=[{"id": "f1", "file_name": "甲.pdf", "version": 2, "file_hash": "h2", "page_count": 4}],
    )
    return {"project": project, "old": old, "new": new}


def test_task_diff_detects_input_and_evidence_changes(db, world):
    old_pair = _pair(db, world["old"], overall_risk="low")
    new_pair = _pair(db, world["new"], overall_risk="high")
    _evidence(db, world["old"], old_pair, chunk_a="a1", chunk_b="b1", severity="medium", summary="旧证据")
    _evidence(db, world["new"], new_pair, chunk_a="a1", chunk_b="b1", severity="high", summary="旧证据")
    _evidence(db, world["new"], new_pair, chunk_a="a2", chunk_b="b2", severity="medium", summary="新证据")

    diff = results_service.task_diff(db, world["project"], world["new"].id)

    assert diff["previous"]["task_no"] == 1
    assert diff["inputs"]["files_changed"][0]["old_version"] == 1
    assert diff["inputs"]["files_changed"][0]["new_version"] == 2
    assert diff["inputs"]["rule_changes"][0]["new_version"] == 2
    assert diff["summary"]["added"] == 1
    assert diff["summary"]["changed"] == 1
    assert diff["risks"][0]["old_risk"] == "low"
    assert diff["risks"][0]["new_risk"] == "high"


def test_task_diff_reports_reappeared_excluded(db, world):
    old_pair = _pair(db, world["old"])
    new_pair = _pair(db, world["new"])
    _evidence(
        db,
        world["old"],
        old_pair,
        chunk_a="a1",
        chunk_b="b1",
        severity="medium",
        summary="甲《甲.pdf》第 2 页与 乙《乙.pdf》第 2 页存在 120 字相似文本",
        review="excluded",
    )
    # 文件替换后文本块重切，块 id 变了，但内容摘要一致
    _evidence(
        db,
        world["new"],
        new_pair,
        chunk_a="a9",
        chunk_b="b9",
        severity="medium",
        summary="甲《甲.pdf》第 2 页与 乙《乙.pdf》第 2 页存在 120 字相似文本",
    )

    diff = results_service.task_diff(db, world["project"], world["new"].id)

    assert diff["summary"]["reappeared"] == 1
    assert diff["evidences"]["reappeared"][0]["previous_review_status"] == "excluded"


def test_task_diff_requires_previous_task(db):
    project = Project(project_name="单任务项目")
    db.add(project)
    db.commit()
    db.refresh(project)
    only = _task(db, project, 1)
    with pytest.raises(ValueError):
        results_service.task_diff(db, project, only.id)


def test_task_diff_explicit_against(db, world):
    diff = results_service.task_diff(db, world["project"], world["new"].id, world["old"].id)
    assert diff["previous"]["id"] == world["old"].id
