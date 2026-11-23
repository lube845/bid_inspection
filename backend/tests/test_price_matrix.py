"""价格比对矩阵（`?tab=price-matrix`）的回归测试。"""

from __future__ import annotations

import pytest

from app.analysis.line_items import _parse_row, extract_line_items
from app.analysis.sources import BlockRef
from app.models import (
    AnalysisTask,
    Company,
    DocumentBlock,
    DocumentPage,
    OriginalFile,
    Project,
)
from app.services.price_matrix_service import build_price_matrix


TABLE_A = (
    "序号\t项目名称\t单位\t数量\t单价\t合价\n"
    "1\t土方开挖\tm3\t100\t12.50\t1250\n"
    "2\t混凝土浇筑\tm3\t200\t380.00\t76000\n"
    "3\t钢筋制安\tt\t30\t4200\t126000"
)
TABLE_B = (
    "序号\t项目名称\t单位\t数量\t单价\t合价\n"
    "1\t土方开挖\tm3\t120\t12.50\t1500\n"
    "2\t混凝土浇筑\tm3\t200\t380.00\t76000\n"
    "3\t钢筋制安\tt\t30\t4200\t126000"
)
TABLE_C = (
    "序号\t项目名称\t单位\t数量\t单价\t合价\n"
    "1\t土方开挖\tm3\t90\t13.20\t1188\n"
    "2\t混凝土浇筑\tm3\t180\t385.00\t69300\n"
    "3\t钢筋制安\tt\t28\t5100\t142800"
)


def _block(text: str) -> BlockRef:
    return BlockRef(
        company_id="c1",
        company_name="甲公司",
        file_id="f1",
        file_name="标书.pdf",
        file_type="pdf",
        file_version=1,
        page_index=1,
        printed_page_number=None,
        block_id="b1",
        block_index=0,
        content=text,
        normalized=text,
        block_type="paragraph",
    )


# ---------------------------------------------------------------- 行解析


@pytest.mark.parametrize(
    "line,expected",
    [
        ("1\t土方开挖\tm3\t100\t12.50\t1250", 12.5),
        # 整数单价（工程量清单常见）：没有小数时回退到整数
        ("3\t钢筋制安\tt\t30\t4200\t126000", 4200.0),
        # 有小数时，序号/数量等整数不被当单价
        ("1\t土方开挖\tm3\t100\t12.50", 12.5),
        ("2\t混凝土浇筑\tm3\t200\t380.00", 380.0),
    ],
)
def test_price_column_picks_unit_price(line, expected):
    item = _parse_row(line, "c1", "甲", _block(""))
    assert item is not None
    assert item.unit_price == expected


def test_extract_line_items_keeps_integer_prices():
    items = extract_line_items([_block(TABLE_A)])
    assert items["钢筋制安"].unit_price == 4200.0
    assert set(items) == {"土方开挖", "混凝土浇筑", "钢筋制安"}


# ---------------------------------------------------------------- 矩阵


def _project_with_tables(db, tables: list[str]) -> Project:
    project = Project(project_name="价格矩阵项目")
    db.add(project)
    db.commit()
    companies = [
        Company(project_id=project.id, company_name=name, sort_order=index)
        for index, name in enumerate(["甲公司", "乙公司", "丙公司"])
    ]
    db.add_all(companies)
    db.commit()

    files = []
    for company, text in zip(companies, tables):
        file = OriginalFile(
            project_id=project.id,
            company_id=company.id,
            kind="bid",
            file_name=f"{company.company_name}.txt",
            file_type="txt",
            storage_key="k",
            page_count=1,
            parse_status="success",
        )
        db.add(file)
        db.commit()
        page = DocumentPage(original_file_id=file.id, page_index=1, page_text=text)
        db.add(page)
        db.flush()
        db.add(
            DocumentBlock(
                document_page_id=page.id,
                block_index=0,
                block_type="paragraph",
                content=text,
                normalized_content=text,
            )
        )
        db.commit()
        files.append(file)

    task = AnalysisTask(
        project_id=project.id,
        task_no=1,
        status="completed",
        analysis_config={"dimensions": ["price"], "thresholds": {}, "price_snapshot": {"entries": {}}},
        company_snapshot={
            "companies": [
                {
                    "company_id": company.id,
                    "company_name": company.company_name,
                    "file_ids": [file.id],
                }
                for company, file in zip(companies, files)
            ]
        },
        algorithm_version="test",
        progress={},
    )
    db.add(task)
    db.commit()
    return project


def test_price_matrix_marks_identical_prices(db):
    project = _project_with_tables(db, [TABLE_A, TABLE_B, TABLE_C])
    data = build_price_matrix(db, project)

    assert data["summary"]["row_count"] == 3
    assert data["summary"]["cell_count"] == 9
    rows = {row["name"]: row for row in data["rows"]}

    # 甲、乙单价一致，丙不同：一致的单元格标 identical，丙标为同行价
    soil = rows["土方开挖"]
    statuses = {cell["company_id"]: cell["status"] for cell in soil["cells"]}
    assert sorted(statuses.values()) == ["identical", "identical", "peer"]

    # 悬浮提示所需的同行价与来源都在
    identical_cell = next(cell for cell in soil["cells"] if cell["status"] == "identical")
    assert 12.5 in identical_cell["peers"]
    assert identical_cell["source"]["file_name"] == "甲公司.txt"


def test_price_matrix_flags_outlier_when_peers_agree(db):
    """两家一致、第三家明显偏离：偏离的那家标 outlier。"""
    outlier_table = TABLE_C.replace("13.20", "99.90")
    project = _project_with_tables(db, [TABLE_A, TABLE_B, outlier_table])
    data = build_price_matrix(db, project)
    soil = next(row for row in data["rows"] if row["name"] == "土方开挖")
    by_company = {cell["company_id"]: cell["status"] for cell in soil["cells"]}
    assert "outlier" in by_company.values()
    assert list(by_company.values()).count("identical") == 2


def test_price_matrix_handles_two_companies_without_false_outliers(db):
    """两家企业价格不同只是常态，不能都标成偏离。"""
    project = _project_with_tables(db, [TABLE_A, TABLE_C])
    data = build_price_matrix(db, project)
    statuses = {
        cell["status"]
        for row in data["rows"]
        for cell in row["cells"]
        if cell["amount"] is not None
    }
    assert "outlier" not in statuses


def test_price_matrix_without_task_returns_hint(db):
    project = Project(project_name="空项目")
    db.add(project)
    db.commit()
    data = build_price_matrix(db, project)
    assert data["rows"] == []
    assert data["task"] is None
    assert "价格 Excel" in data["reason"]


def test_imported_price_items_take_priority_over_text(db):
    """上传价格 Excel 后，矩阵以 Excel 为准，而不是正文识别。"""
    from app.models import PriceItem

    project = _project_with_tables(db, [TABLE_A, TABLE_B, TABLE_C])
    company = db.query(Company).filter(Company.project_id == project.id).order_by(
        Company.sort_order
    ).first()
    # 甲公司的「土方开挖」在正文里是 12.50，Excel 里改成 88.80
    db.add(
        PriceItem(
            project_id=project.id,
            company_id=company.id,
            item_key="土方开挖",
            item_name="土方开挖",
            unit_price=88.8,
            source_kind="import",
            source_ref={"filename": "甲公司价格.xlsx"},
            version_no=1,
            is_active=True,
        )
    )
    db.commit()

    data = build_price_matrix(db, project)
    soil = next(row for row in data["rows"] if row["name"] == "土方开挖")
    target = next(cell for cell in soil["cells"] if cell["company_id"] == company.id)
    assert target["amount"] == 88.8
    assert target["source"]["source_kind"] == "import"
