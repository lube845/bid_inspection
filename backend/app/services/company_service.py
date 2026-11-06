from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Company, Project, User
from app.models.base import utcnow
from app.models.enums import CompanyStatus, TaskStatus
from app.services import audit, project_service


class CompanyError(Exception):
    pass


def create_company(
    db: Session, project: Project, company_name: str, credit_code: str | None, remark: str | None,
    actor: User | None = None,
) -> Company:
    name = company_name.strip()
    if not name:
        raise CompanyError("企业名称不能为空")
    count = db.scalar(
        select(func.count())
        .select_from(Company)
        .where(Company.project_id == project.id, Company.deleted_at.is_(None))
    ) or 0
    if count >= settings.max_companies_per_project:
        raise CompanyError(f"当前项目最多支持 {settings.max_companies_per_project} 家投标企业")
    duplicate = db.scalar(
        select(Company.id).where(
            Company.project_id == project.id,
            Company.company_name == name,
            Company.deleted_at.is_(None),
        )
    )
    if duplicate:
        raise CompanyError("同一项目内企业名称不能重复")

    max_order = db.scalar(
        select(func.max(Company.sort_order)).where(
            Company.project_id == project.id, Company.deleted_at.is_(None)
        )
    )
    company = Company(
        project_id=project.id,
        company_name=name,
        credit_code=credit_code,
        remark=remark,
        sort_order=(max_order or 0) + 1,
        status=CompanyStatus.INCOMPLETE.value,
        participate_analysis=True,
    )
    db.add(company)
    db.flush()
    audit.log_audit(db, "company.create", "company", company.id, actor, {"name": name})
    project_service.refresh_project_status(db, project)
    db.commit()
    db.refresh(company)
    return company


def update_company(db: Session, company: Company, payload, actor: User | None = None) -> Company:
    data = payload.model_dump(exclude_unset=True)
    if "company_name" in data and data["company_name"] is not None:
        name = data["company_name"].strip()
        if not name:
            raise CompanyError("企业名称不能为空")
        duplicate = db.scalar(
            select(Company.id).where(
                Company.project_id == company.project_id,
                Company.company_name == name,
                Company.id != company.id,
                Company.deleted_at.is_(None),
            )
        )
        if duplicate:
            raise CompanyError("同一项目内企业名称不能重复")
        data["company_name"] = name
    for key, value in data.items():
        setattr(company, key, value)
    audit.log_audit(db, "company.update", "company", company.id, actor, data)
    project = db.get(Project, company.project_id)
    if project:
        project_service.refresh_project_status(db, project)
    db.commit()
    db.refresh(company)
    return company


def delete_company(db: Session, company: Company, actor: User | None = None) -> None:
    from app.models import AnalysisTask

    running = db.scalar(
        select(AnalysisTask.id)
        .where(
            AnalysisTask.project_id == company.project_id,
            AnalysisTask.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
        )
        .limit(1)
    )
    if running:
        raise CompanyError("项目有分析任务正在执行，暂不能删除企业")

    # 软删除企业时同步停用其文件，避免继续计入项目统计；历史分析快照按文件 ID 仍可追溯。
    for file in company.files:
        file.is_active = False
    company.deleted_at = utcnow()
    company.participate_analysis = False
    company.status = CompanyStatus.INCOMPLETE.value
    audit.log_audit(db, "company.delete", "company", company.id, actor, {"name": company.company_name})
    project = db.get(Project, company.project_id)
    if project:
        project_service.refresh_project_status(db, project)
    db.commit()
