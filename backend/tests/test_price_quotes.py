"""报价结构化导入、版本化与任务快照。

覆盖验收报告里的阻断项：报价识别不到时不再静默无结论，人工导入/补录成为
权威输入，且历史任务在报价被修改后仍可复现。
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.db.session import SessionLocal
from app.models import Company, PriceQuote, Project
from app.models.enums import FileKind
from app.services import analysis_service, quote_service
from app.services.quote_service import QuoteError


def _project(db, name: str = "报价测试项目") -> Project:
    project = Project(project_name=name, project_code="QT-001")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _company(db, project: Project, name: str, code: str | None = None) -> Company:
    company = Company(project_id=project.id, company_name=name, credit_code=code)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


HEADER = ["企业名称", "统一社会信用代码", "投标总报价（元）", "大写金额", "备注"]


def test_set_quote_versions_and_keeps_history(db):
    project = _project(db)
    company = _company(db, project, "甲公司")

    first = quote_service.set_quote(db, project, company, 1_000_000)
    second = quote_service.set_quote(db, project, company, 1_100_000)

    assert first.version_no == 1
    assert second.version_no == 2
    rows = list(db.query(PriceQuote).filter(PriceQuote.company_id == company.id))
    assert len(rows) == 2
    assert sum(1 for row in rows if row.is_active) == 1
    assert [row.is_active for row in rows if row.version_no == 1] == [False]


def test_import_dry_run_does_not_write(db):
    project = _project(db)
    _company(db, project, "甲公司", "91310000AAA")
    data = _xlsx([HEADER, ["甲公司", "91310000AAA", 9_986_000, "玖佰玖拾捌万陆仟元整", ""]])

    result = quote_service.import_quotes(db, project, "quotes.xlsx", data, dry_run=True)

    assert result["summary"]["total"] == 1
    assert result["errors"] == []
    assert db.query(PriceQuote).count() == 0


def test_import_matches_by_credit_code_and_commits(db):
    project = _project(db)
    company = _company(db, project, "甲公司", "91310000AAA")
    data = _xlsx([HEADER, ["名称写错也没关系", "91310000AAA", 8_880_000, "", "备注"]])

    result = quote_service.import_quotes(db, project, "quotes.xlsx", data, dry_run=False)

    assert result["summary"]["total"] == 1
    quote = db.query(PriceQuote).filter(PriceQuote.company_id == company.id).one()
    assert float(quote.amount) == 8_880_000
    assert quote.source_kind == "import"
    assert quote.source_ref["batch_id"]


def test_import_reports_row_errors(db):
    project = _project(db)
    _company(db, project, "甲公司")
    data = _xlsx(
        [
            HEADER,
            ["甲公司", "", -1, "", ""],
            ["不存在的企业", "", 100, "", ""],
        ]
    )

    result = quote_service.import_quotes(db, project, "quotes.xlsx", data, dry_run=True)

    messages = [item["message"] for item in result["errors"]]
    assert any("大于 0" in message for message in messages)
    assert any("未找到企业" in message for message in messages)
    assert result["summary"]["total"] == 0


def test_import_rejects_case_mismatch(db):
    project = _project(db)
    _company(db, project, "甲公司")
    data = _xlsx([HEADER, ["甲公司", "", 1_000_000, "贰佰万元整", ""]])

    result = quote_service.import_quotes(db, project, "quotes.xlsx", data, dry_run=True)

    assert result["summary"]["total"] == 0
    assert any("大小写金额不一致" in item["message"] for item in result["errors"])


def test_import_rejects_missing_header(db):
    project = _project(db)
    with pytest.raises(QuoteError):
        quote_service.import_quotes(
            db, project, "quotes.xlsx", _xlsx([["随便", "什么"]]), dry_run=True
        )


def test_manual_quote_wins_in_price_snapshot(db):
    """人工报价优先于正文自动抽取，且固化进任务快照。"""
    project = _project(db)
    company = _company(db, project, "甲公司")
    quote_service.set_quote(db, project, company, 5_000_000)

    snapshot = analysis_service._build_manual_price_snapshot(db, project.id, [company])

    entry = snapshot["entries"][company.id]
    assert entry["amount"] == 5_000_000
    assert entry["source_kind"] == "manual"
    assert entry["source"]["locator_precision"] == "quote"


def test_stale_price_quotes_detects_new_version(db):
    project = _project(db)
    company = _company(db, project, "甲公司")
    quote_service.set_quote(db, project, company, 5_000_000)

    from app.models import AnalysisTask
    from app.models.enums import TaskStatus

    task = AnalysisTask(
        project_id=project.id,
        task_no=1,
        status=TaskStatus.COMPLETED.value,
        analysis_config={
            "price_snapshot": analysis_service._build_manual_price_snapshot(
                db, project.id, [company]
            )
        },
        company_snapshot={"companies": []},
        algorithm_version="test",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    assert analysis_service._stale_price_quotes(db, task) == []

    quote_service.set_quote(db, project, company, 6_000_000)
    stale = analysis_service._stale_price_quotes(db, task)
    assert len(stale) == 1
    assert stale[0]["snapshot_version"] == 1
    assert stale[0]["current_version"] == 2
