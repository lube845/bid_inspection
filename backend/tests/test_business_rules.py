import pytest

from app.analysis.price import amount_to_upper
from app.models import Project
from app.services import company_service
from app.services.company_service import CompanyError
from app.utils.text import normalize, normalize_with_map


def test_company_limit_is_enforced(db):
    project = Project(project_name="限额测试项目", status="pending")
    db.add(project)
    db.commit()

    for index in range(20):
        company_service.create_company(db, project, f"测试企业{index + 1:02d}", None, None)
    assert len(project.companies) == 20

    with pytest.raises(CompanyError):
        company_service.create_company(db, project, "第21家企业", None, None)


def test_duplicate_company_name_is_rejected(db):
    project = Project(project_name="重名测试", status="pending")
    db.add(project)
    db.commit()
    company_service.create_company(db, project, "同名企业", None, None)
    with pytest.raises(CompanyError):
        company_service.create_company(db, project, "同名企业", None, None)


def test_normalized_offset_mapping():
    raw = "项目负责人：张 伟（项目经理）"
    normalized, index_map = normalize_with_map(raw)
    assert " " not in normalized
    assert "：" not in normalized
    position = normalized.index("张伟")
    assert raw[index_map[position]] == "张"
    assert raw[index_map[position + 1]] == "伟"
    assert normalize("ＡＢＣ　１２３") == "abc123"


def test_amount_to_upper():
    assert "元" in amount_to_upper(10_000_000)
    assert amount_to_upper(0) == "零元整"
