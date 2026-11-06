"""工作台（PRD 4.1）测试。

重点覆盖「同一证据不得重复计数」：涉及 3 家企业的项目级价格证据只能算 1 条，
被重跑标记为 is_current=False 的历史证据不计入。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import (
    AnalysisTask,
    Company,
    ComparisonEvidence,
    EvidenceParticipant,
    OriginalFile,
    Project,
)
from app.models.enums import (
    EvidenceType,
    FileKind,
    FileParseStatus,
    ReviewStatus,
    RiskLevel,
    TaskStatus,
)
from app.models.base import utcnow
from app.services import workbench_service


def _project(db, name, code, status="analyzing"):
    project = Project(project_name=name, project_code=code, status=status)
    db.add(project)
    db.commit()
    return project


def _company(db, project, name):
    company = Company(project_id=project.id, company_name=name, participate_analysis=True)
    db.add(company)
    db.commit()
    return company


def _file(db, project, company, name, parse_status):
    record = OriginalFile(
        project_id=project.id,
        company_id=company.id if company else None,
        kind=FileKind.BID.value,
        file_name=name,
        file_type="txt",
        storage_key=f"{project.id}/{name}",
        file_size=10,
        file_hash="hash-" + name,
        version=1,
        page_count=1,
        parse_status=parse_status,
        sort_order=0,
        is_active=True,
        uploaded_at=utcnow(),
    )
    db.add(record)
    db.commit()
    return record


def _evidence(db, project, task, evidence_type, severity="medium", is_current=True, participants=()):
    record = ComparisonEvidence(
        project_id=project.id,
        analysis_task_id=task.id,
        comparison_pair_id=None,
        evidence_type=evidence_type,
        evidence_subtype=f"{evidence_type}_subtype",
        severity=severity,
        summary=f"{evidence_type} 证据",
        evidence_data={},
        source_a=None,
        source_b=None,
        algorithm_version="test",
        review_status=ReviewStatus.PENDING.value,
        is_current=is_current,
    )
    db.add(record)
    db.flush()
    for company_id, company_name in participants:
        db.add(
            EvidenceParticipant(
                evidence_id=record.id,
                company_id=company_id,
                company_name=company_name,
                role="participant",
                source=None,
            )
        )
    db.commit()
    return record


def _task(db, project, task_no=1, status=TaskStatus.COMPLETED.value):
    task = AnalysisTask(
        project_id=project.id,
        task_no=task_no,
        status=status,
        analysis_config={"dimensions": ["text"], "thresholds": {}},
        company_snapshot={"companies": []},
        algorithm_version="test",
        progress={},
        created_by=None,
    )
    db.add(task)
    db.commit()
    return task


def test_workbench_counts_each_evidence_once(db):
    project = _project(db, "统计项目", "DEMO-2026-001")
    task = _task(db, project)
    company_a = _company(db, project, "甲公司")
    company_b = _company(db, project, "乙公司")
    company_c = _company(db, project, "丙公司")

    _evidence(db, project, task, EvidenceType.TEXT.value)
    _evidence(db, project, task, EvidenceType.TEXT.value, severity="high")
    # 涉及 3 家企业的项目级价格证据：只能算 1 条
    _evidence(
        db,
        project,
        task,
        EvidenceType.PRICE.value,
        participants=(
            (company_a.id, "甲公司"),
            (company_b.id, "乙公司"),
            (company_c.id, "丙公司"),
        ),
    )
    # 重跑后被标记为非当前的历史证据，不计入
    _evidence(db, project, task, EvidenceType.TEXT.value, is_current=False)

    summary = workbench_service.summary(db)
    assert summary["stats"]["evidence_total"] == 3
    assert summary["stats"]["evidence_pending_review"] == 3
    assert sum(summary["risk"]["by_type"].values()) == 3
    assert sum(summary["risk"]["by_review_status"].values()) == 3
    assert summary["risk"]["by_type"][EvidenceType.TEXT.value] == 2
    assert summary["risk"]["by_type"][EvidenceType.PRICE.value] == 1


def test_workbench_excludes_deleted_projects(db):
    project = _project(db, "已删除项目", "GONE-001")
    task = _task(db, project)
    _evidence(db, project, task, EvidenceType.TEXT.value)
    project.deleted_at = datetime.now(timezone.utc)
    db.commit()

    summary = workbench_service.summary(db)
    assert summary["stats"]["project_total"] == 0
    assert summary["stats"]["evidence_total"] == 0
    assert summary["stats"]["task_completed"] == 0


def test_workbench_todos_cover_parse_and_analysis_problems(db):
    project = _project(db, "待办项目", "TODO-001")
    task = _task(db, project, status=TaskStatus.FAILED.value)
    company = _company(db, project, "甲公司")
    _file(db, project, company, "损坏文件.txt", FileParseStatus.FAILED.value)
    _file(db, project, company, "部分文件.txt", FileParseStatus.PARTIAL.value)

    summary = workbench_service.summary(db)
    codes = [todo["code"] for todo in summary["todos"]]
    assert "parse_failed" in codes
    assert "parse_partial" in codes
    assert "analysis_failed" in codes

    failed = next(todo for todo in summary["todos"] if todo["code"] == "parse_failed")
    assert failed["project_id"] == project.id
    assert failed["link"] == f"#/projects/{project.id}?tab=parse"
    assert failed["severity"] == "high"


def test_workbench_recent_projects_and_tasks_are_ordered(db):
    older = _project(db, "旧项目", "OLD-001")
    newer = _project(db, "新项目", "NEW-001")
    # 显式设置更新时间，保证排序稳定
    older.updated_at = datetime.now(timezone.utc) - timedelta(days=2)
    newer.updated_at = datetime.now(timezone.utc)
    db.commit()

    task = _task(db, newer, task_no=7)
    task.created_at = datetime.now(timezone.utc)
    db.commit()

    summary = workbench_service.summary(db)
    assert summary["recent_projects"][0]["id"] == newer.id
    assert summary["recent_tasks"][0]["task_no"] == 7
    assert summary["recent_tasks"][0]["project_name"] == "新项目"


def test_workbench_endpoint_requires_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/v1/workbench").status_code == 401
