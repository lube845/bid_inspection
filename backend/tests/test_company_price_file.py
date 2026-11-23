"""单企业价格 Excel 的解析与导入（文件管理 → 上传价格 Excel）。"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.models import Company, Project
from app.services import company_price_service, quote_service
from app.services.company_price_service import parse_company_price_file
from app.services.quote_service import QuoteError


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


TABLE = [
    ["项目名称", "单位", "数量", "单价", "合价"],
    ["土方开挖", "m3", 100, 12.5, 1250],
    ["混凝土浇筑", "m3", 200, 380, 76000],
    ["钢筋制安", "t", 30, 4200, 126000],
    ["合计", "", "", "", 203250],
]


def test_parse_items_and_total():
    result = parse_company_price_file("price.xlsx", _xlsx(TABLE))
    assert result["total_amount"] == 203250
    assert [item["name"] for item in result["items"]] == ["土方开挖", "混凝土浇筑", "钢筋制安"]
    assert result["items"][2]["unit_price"] == 4200
    assert result["errors"] == []


def test_parse_total_only_file():
    data = _xlsx([["投标总报价（元）"], [10000000]])
    result = parse_company_price_file("total.xlsx", data)
    assert result["total_amount"] == 10_000_000
    assert result["items"] == []
    assert any("分项清单" in w["message"] for w in result["warnings"])


def test_parse_infers_total_from_quantity_price():
    rows = [
        ["项目名称", "数量", "单价"],
        ["土方开挖", 100, 12.5],
        ["混凝土浇筑", 200, 380],
    ]
    result = parse_company_price_file("infer.xlsx", _xlsx(rows))
    assert result["total_amount"] == pytest.approx(77250)
    assert any("推算" in w["message"] for w in result["warnings"])


def test_parse_rejects_unrecognized_table():
    with pytest.raises(QuoteError):
        parse_company_price_file("bad.xlsx", _xlsx([["随便写点", "别的"]]))


def test_parse_skips_duplicate_items_with_warning():
    rows = [
        ["项目名称", "单价"],
        ["土方开挖", 12.5],
        ["土方开挖", 13.0],
    ]
    result = parse_company_price_file("dup.xlsx", _xlsx(rows))
    assert len(result["items"]) == 1
    assert result["items"][0]["unit_price"] == 12.5
    assert any("重复" in w["message"] for w in result["warnings"])


def test_import_writes_total_and_items_and_supersedes(db):
    project = Project(project_name="价格导入项目")
    db.add(project)
    db.commit()
    company = Company(project_id=project.id, company_name="甲公司")
    db.add(company)
    db.commit()

    parsed = parse_company_price_file("price.xlsx", _xlsx(TABLE))
    quote_service.set_quote(
        db, project, company, parsed["total_amount"], source_kind="import"
    )
    first = quote_service.set_price_items(
        db, project, company, parsed["items"], source_kind="import"
    )
    assert len(first) == 3

    # 重新导入：旧版本置为无效，新版本号 +1
    second = quote_service.set_price_items(
        db, project, company, parsed["items"][:2], source_kind="import"
    )
    assert second[0].version_no == 2
    active = quote_service.list_price_items(db, project)[company.id]
    assert len(active) == 2
    assert all(row["version_no"] == 2 for row in active)


def test_price_items_are_normalized_for_cross_company_alignment(db):
    project = Project(project_name="对齐项目")
    db.add(project)
    db.commit()
    company = Company(project_id=project.id, company_name="甲公司")
    db.add(company)
    db.commit()

    items = [
        {"name": "土方开挖（含运输）", "key": "土方开挖含运输", "unit_price": 12.5},
        # 只传名称时服务端负责规范化
        {"name": "混凝土 浇筑", "unit_price": 380},
    ]
    quote_service.set_price_items(db, project, company, items)
    active = quote_service.list_price_items(db, project)[company.id]
    keys = {row["key"] for row in active}
    assert "土方开挖含运输" in keys
    assert "混凝土浇筑" in keys
