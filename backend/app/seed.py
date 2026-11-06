"""演示数据：一键生成一个可完整走通业务流程的项目。

仅在 SEED_DEMO=true 且项目不存在时执行，重复启动不会重复造数。
"""

from __future__ import annotations

import hashlib
import os
import tempfile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnalysisTask, Company, OriginalFile, Project
from app.models.enums import (
    FileKind,
    FileParseStatus,
    ProjectStatus,
    TaskStatus,
)
from app.models.base import utcnow
from app.schemas.analysis import StartAnalysisRequest
from app.services import analysis_service, file_service, project_service
from app.storage.local import get_storage

_DEMO_CODE = "DEMO-2026-001"

_SHARED_PLAN = (
    "一、项目实施方案。我方将严格按照招标文件要求，结合项目实际情况，科学组织施工，"
    "合理配置人员、设备和材料，确保工程按期保质完成。项目实施过程中建立项目经理负责制，"
    "明确各岗位职责，实行进度、质量、安全、成本全过程管理。针对关键工序编制专项施工方案，"
    "并在实施前组织技术交底和风险评审。"
)
_SHARED_QUALITY = (
    "二、质量管理措施。建立健全质量管理体系，实行三级质量检查制度，坚持自检、互检、"
    "专检相结合。所有进场材料必须提供合格证明并按规定见证取样送检，未经检验合格的材料"
    "不得用于本工程。隐蔽工程验收合格后方可进入下一道工序，质量记录完整归档，确保"
    "工程质量达到国家现行验收标准。"
)
_SHARED_SERVICE = (
    "三、售后服务承诺。本公司承诺提供7×24小时售后服务响应，接到通知后2小时内到达现场，"
    "一般问题24小时内解决，重大问题48小时内提出解决方案。质保期内免费提供维修和更换服务，"
    "质保期满后继续提供终身维护支持。"
)
_TENDER_PUBLIC = (
    "投标人应当按照招标文件规定的格式编制投标文件，投标文件应当对招标文件提出的"
    "实质性要求和条件作出响应。投标文件包括商务部分、技术部分和报价部分，投标人应"
    "严格按照评标办法和评分标准组织响应内容，并对所提供的全部资料的真实性负责。"
)

_COMPANY_A = f"""投标总报价：10,000,000.00 元
大写：人民币壹仟万元整
项目负责人：张伟
技术负责人：李娜
授权代表：王强

{_SHARED_PLAN}

{_SHARED_QUALITY}

{_TENDER_PUBLIC}

{_SHARED_SERVICE}
"""

_COMPANY_B = f"""投标报价：10,050,000.00 元
大写：人民币壹仟零伍万元整
项目负责人：张伟
技术负责人：李娜
造价人员：赵敏

{_SHARED_PLAN}

{_SHARED_QUALITY}

{_TENDER_PUBLIC}

四、安全文明施工措施。严格执行安全生产责任制，落实安全教育培训、隐患排查和应急演练，
确保施工期间不发生重大安全事故。
"""

_COMPANY_C = f"""投标总价：9,950,000.00 元
大写：人民币玖佰玖拾伍万元整
项目负责人：王强
技术负责人：陈晨
预算员：赵敏

{_SHARED_PLAN[:180]}

五、企业类似项目业绩。近年来完成多项同类项目，合同履约情况良好，未发生重大质量安全
事故，具备完成本项目所需的技术能力和管理经验。
"""


def _create_seed_file(
    db: Session, project: Project, company: Company | None, name: str, content: str, kind: str, order: int
) -> OriginalFile:
    raw = content.encode("utf-8")
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    temp.write(raw)
    temp.close()
    storage_key = f"{project.id}/{os.urandom(16).hex()}.txt"
    get_storage().save_from_path(temp.name, storage_key)
    record = OriginalFile(
        project_id=project.id,
        company_id=company.id if company else None,
        kind=kind,
        file_name=name,
        file_type="txt",
        storage_key=storage_key,
        file_size=len(raw),
        file_hash=hashlib.sha256(raw).hexdigest(),
        version=1,
        page_count=0,
        parse_status=FileParseStatus.PENDING.value,
        sort_order=order,
        is_active=True,
        uploaded_at=utcnow(),
    )
    db.add(record)
    db.flush()
    return record


def seed_demo(db: Session) -> None:
    existing = db.scalars(
        select(Project).where(Project.project_code == _DEMO_CODE, Project.deleted_at.is_(None))
    ).first()
    if existing:
        return

    project = Project(
        project_name="演示采购项目A",
        project_code=_DEMO_CODE,
        description="系统内置演示项目，覆盖文件解析、企业两两比对、三栏证据与人工审核流程。",
        status=ProjectStatus.PENDING.value,
    )
    db.add(project)
    db.flush()

    tender = _create_seed_file(
        db, project, None, "招标文件.txt", _TENDER_PUBLIC, FileKind.TENDER.value, 0
    )

    companies = []
    for index, name in enumerate(["贵州甲建设有限公司", "贵州乙工程有限公司", "贵州丙科技有限公司"]):
        company = Company(
            project_id=project.id,
            company_name=name,
            sort_order=index,
            status="incomplete",
            participate_analysis=True,
        )
        db.add(company)
        companies.append(company)
    db.flush()

    file_a = _create_seed_file(db, project, companies[0], "商务标.txt", _COMPANY_A, FileKind.BID.value, 0)
    file_b = _create_seed_file(db, project, companies[1], "技术标.txt", _COMPANY_B, FileKind.BID.value, 0)
    file_c = _create_seed_file(db, project, companies[2], "报价清单.txt", _COMPANY_C, FileKind.BID.value, 0)
    db.commit()

    file_service.parse_file(db, tender.id, auto_start=False)
    for record in (file_a, file_b, file_c):
        file_service.parse_file(db, record.id, auto_start=False)
    for company in companies:
        file_service.confirm_company(db, company)

    payload = StartAnalysisRequest(
        company_ids=[c.id for c in companies],
        dimensions=["text", "personnel", "price"],
        exclude_tender_public_content=True,
        confirm_partial=True,
    )
    project = db.get(Project, project.id)
    task = analysis_service.create_task(db, project, payload, None)
    analysis_service.run_task(db, task.id)

    task = db.get(AnalysisTask, task.id)
    if task and task.status == TaskStatus.QUEUED.value:
        task.status = TaskStatus.FAILED.value
    db.commit()
    project_service.refresh_project_status(db, db.get(Project, project.id))
    db.commit()
