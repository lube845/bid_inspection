import pytest

from app.analysis.price import amount_to_upper
from app.analysis.risk import text_risk
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


def test_text_risk_requires_count_and_similarity_together():
    """规则文案是「条数与相似度同时满足」：条数够但相似度低不应判中风险。"""
    level, _ = text_risk(3, 0.55)
    assert level == "low"
    level, _ = text_risk(3, 0.86)
    assert level == "medium"
    level, _ = text_risk(5, 0.96)
    assert level == "high"


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("金额：￥100万元", "金额100万元"),
        ("金额:100万元", "金额100万元"),
        ("利率 5%", "利率5"),
        ("利率 5％", "利率5"),
        ("2024/01/01", "20240101"),
        ("2024-01-01", "20240101"),
        ("| 项目 | 名称", "项目名称"),
        ("A&B 有限公司", "ab有限公司"),
    ],
)
def test_normalization_ignores_layout_symbols(raw, expected):
    """纯排版符号（货币、百分号、表格竖线、日期分隔符）不参与相似度判定。"""
    assert normalize(raw) == expected


def test_normalization_keeps_digits_and_chinese():
    """数字与汉字必须保留，否则金额、编号会互相撞车。"""
    assert normalize("中标价 1,234,567.00 元") == "中标价123456700元"
    assert normalize("第3章 项目管理") == "第3章项目管理"


def test_amount_to_upper():
    assert "元" in amount_to_upper(10_000_000)
    assert amount_to_upper(0) == "零元整"
