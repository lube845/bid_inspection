import hashlib
import os
import tempfile

from sqlalchemy import select

from app.models import AnalysisTask, Company, OriginalFile, Project
from app.models.enums import FileKind, FileParseStatus, TaskStatus
from app.models.base import utcnow
from app.services import file_service
from app.storage.local import get_storage

_CONTENT = """投标总报价：10,000,000.00 元
大写：人民币壹仟万元整
项目负责人：张伟
技术负责人：李娜

一、项目实施方案。我方将严格按照招标文件要求，结合项目实际情况，科学组织施工，
合理配置人员、设备和材料，确保工程按期保质完成。
"""


def _make_file(db, project: Project, company: Company | None, name: str) -> OriginalFile:
    raw = _CONTENT.encode("utf-8")
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    temp.write(raw)
    temp.close()
    storage_key = f"{project.id}/{os.urandom(16).hex()}.txt"
    get_storage().save_from_path(temp.name, storage_key)
    record = OriginalFile(
        project_id=project.id,
        company_id=company.id if company else None,
        kind=FileKind.BID.value,
        file_name=name,
        file_type="txt",
        storage_key=storage_key,
        file_size=len(raw),
        file_hash=hashlib.sha256(raw).hexdigest(),
        version=1,
        page_count=0,
        parse_status=FileParseStatus.PENDING.value,
        sort_order=0,
        is_active=True,
        uploaded_at=utcnow(),
    )
    db.add(record)
    db.commit()
    return record


def test_analysis_starts_automatically_when_all_files_parsed(db):
    project = Project(project_name="自动比对项目", project_code="AUTO-001")
    db.add(project)
    db.commit()
    company_a = Company(project_id=project.id, company_name="甲公司", participate_analysis=True)
    company_b = Company(project_id=project.id, company_name="乙公司", participate_analysis=True)
    db.add_all([company_a, company_b])
    db.commit()

    file_a = _make_file(db, project, company_a, "甲公司.txt")
    file_b = _make_file(db, project, company_b, "乙公司.txt")

    # 只有一家企业解析完成时不发起比对
    file_service.parse_file(db, file_a.id)
    assert db.scalars(select(AnalysisTask)).first() is None

    # 第二家解析完成后自动发起两两比对
    file_service.parse_file(db, file_b.id)
    tasks = list(db.scalars(select(AnalysisTask).order_by(AnalysisTask.task_no)))
    assert len(tasks) == 1
    task = tasks[0]
    assert task.status in {TaskStatus.COMPLETED.value, TaskStatus.PARTIAL.value}
    assert task.analysis_config["dimensions"] == ["text", "personnel", "price"]
    assert task.analysis_config["exclude_tender_public_content"] is True
    assert len(task.company_snapshot["companies"]) == 2


def test_new_file_triggers_a_fresh_comparison(db):
    project = Project(project_name="增量比对项目", project_code="AUTO-002")
    db.add(project)
    db.commit()
    company_a = Company(project_id=project.id, company_name="甲公司", participate_analysis=True)
    company_b = Company(project_id=project.id, company_name="乙公司", participate_analysis=True)
    db.add_all([company_a, company_b])
    db.commit()

    file_a = _make_file(db, project, company_a, "甲公司.txt")
    file_service.parse_file(db, file_a.id)
    file_service.parse_file(db, _make_file(db, project, company_b, "乙公司.txt").id)
    assert len(list(db.scalars(select(AnalysisTask)))) == 1

    # 重新解析同一份文件，比对范围未变，不重复发起
    file_service.parse_file(db, file_a.id)
    assert len(list(db.scalars(select(AnalysisTask)))) == 1

    # 新增文件后比对范围变化，自动发起新一轮比对
    file_service.parse_file(db, _make_file(db, project, company_a, "甲公司补充.txt").id)
    tasks = list(db.scalars(select(AnalysisTask).order_by(AnalysisTask.task_no)))
    assert len(tasks) == 2
    assert tasks[1].task_no == 2
    assert len(tasks[1].company_snapshot["companies"][0]["files"]) == 2
