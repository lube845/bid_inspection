"""QA 报告（prd_kimi_test.md）缺陷的回归测试。

覆盖：
- 公网默认口令提示与令牌撤销（退出登录后旧令牌立即失效）；
- 重复分析时只展示最新任务证据，历史证据不进入待办；
- 带千分位 / 全角 / 表格合计行的投标总价能识别；
- 统一社会信用代码格式与校验位；
- 尾数集中规则忽略整数万元；关键岗位范围参数真正生效。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.analysis.price import Quote, analyze_prices, extract_quote
from app.analysis.sources import BlockRef
from app.models import AnalysisTask, Company, ComparisonEvidence, Project
from app.services import company_service, risk_service
from app.services.company_service import CompanyError
from app.utils.credit_code import credit_code_error


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


# ---------------------------------------------------------------- 价格识别


@pytest.mark.parametrize(
    "text,expected",
    [
        ("投标总价（人民币）：1,400,000.00 元", 1_400_000),
        ("投标报价（人民币）：1400000", 1_400_000),
        ("投标总报价：1,400,000.00元", 1_400_000),
        ("投标总报价（人民币元）：9986000", 9_986_000),
        # 全角数字
        ("投标总报价：１４０００００", 1_400_000),
        # 表格合计行
        ("合计 | 1,400,000.00 元", 1_400_000),
        ("1,400,000.00 元（合计）", 1_400_000),
        ("投标总报价（大写）：壹仟万元整", 10_000_000),
    ],
)
def test_total_quote_supports_thousands_and_table_rows(text, expected):
    quote = extract_quote("c1", "甲公司", [_block(text)])
    assert quote is not None, text
    assert quote.amount == expected, text


def test_table_total_uses_total_row_not_previous_item():
    """QA 第二轮：分项 145,000 不能盖过紧邻的合计 1,450,000。"""
    layouts = [
        "安装调试及培训服务\t145,000.00\n合计\t1,450,000.00",
        "安装调试及培训服务\n145,000.00\n合计（元）\n1,450,000.00",
        "安装调试及培训服务 145,000.00\n合 计\n1,450,000.00",
        "合计（元）1,450,000.00",
    ]
    for text in layouts:
        quote = extract_quote("c1", "甲公司", [_block(text)])
        assert quote is not None, text
        assert quote.amount == 1_450_000, text


def test_conflicting_totals_are_flagged_not_silently_used():
    from app.analysis.price import analyze_quote_quality, quote_from_snapshot

    text = "合计（元）1,400,000.00\n合计（元）1,450,000.00"
    quote = extract_quote("c1", "甲公司", [_block(text)])
    assert quote is not None
    assert quote.inconsistent is True

    findings = analyze_quote_quality([quote])
    assert any(finding.rule == "quote_needs_confirmation" for finding in findings)

    # 快照还原后仍然带待确认标记
    restored = quote_from_snapshot(
        {
            "company_id": "c1",
            "company_name": "甲公司",
            "amount": quote.amount,
            "source_kind": "auto",
            "needs_confirmation": quote.inconsistent,
        }
    )
    assert restored is not None and restored.inconsistent is True


def test_itemized_amount_does_not_override_total():
    """分项「报价金额」不能盖过真正的投标总报价。"""
    blocks = [
        _block("分项报价表\n报价金额：120000元\n设备采购费\n报价金额：80000元"),
        _block("开标一览表\n投标总报价：1.5亿元\n大写：人民币壹亿伍仟万元整"),
    ]
    quote = extract_quote("c1", "甲公司", blocks)
    assert quote is not None
    assert quote.amount == 150_000_000


# ---------------------------------------------------------------- 尾数噪声


def _quote(amount: float, name: str) -> Quote:
    return Quote(
        company_id=name,
        company_name=name,
        amount=amount,
        raw=str(amount),
        upper=None,
        block=None,
        start=0,
        end=0,
    )


def test_cover_page_text_is_not_counted_as_risk():
    from app.analysis.text_similarity import _cover_only

    assert _cover_only("投标文件\n项目名称：某工程\n投标人：甲公司\n日期：2026年1月1日")
    assert not _cover_only(
        "我方将严格按照招标文件要求，结合项目实际情况，科学组织施工，合理配置人员、设备和材料，"
        "确保工程按期保质完成。项目实施过程中建立项目经理负责制，明确各岗位职责。"
    )


def test_key_positions_parameter_filters_people():
    from app.analysis.personnel import extract_people

    blocks = [
        _block("项目负责人：张伟"),
        _block("法定代表人：李强"),
    ]
    # 不带参数：两个岗位都抽取
    assert {occ.name for occ in extract_people(blocks)} == {"张伟", "李强"}
    # 只保留关键岗位时，法定代表人被过滤掉
    people = extract_people(blocks, key_positions=["项目负责人"])
    assert {occ.name for occ in people} == {"张伟"}


def test_round_wan_quotes_do_not_trigger_tail_concentration():
    quotes = [_quote(1_400_000, "甲"), _quote(1_400_000, "乙"), _quote(1_850_000, "丙")]
    rules = {"special_tail": ["888", "999", "998"], "tail_concentration_min_count": 3}
    findings = analyze_prices(quotes, thresholds=rules)
    assert all(finding.rule != "tail_concentration" for finding in findings)


# ---------------------------------------------------------------- 信用代码


def test_credit_code_validation():
    assert credit_code_error(None) is None
    assert credit_code_error("91350100M000100Y43") is None
    # 长度不对
    assert credit_code_error("91310000AAA") is not None
    # 校验位不对（QA 报告里的异常样例）
    assert credit_code_error("91320000QA0000001X") is not None
    # 非法字符
    assert credit_code_error("91350582MA34A0IO2B") is not None


def test_company_create_rejects_bad_credit_code(db):
    project = Project(project_name="信用代码项目")
    db.add(project)
    db.commit()

    with pytest.raises(CompanyError):
        company_service.create_company(db, project, "甲公司", "91320000QA0000001X", None)

    # 其他证件类型显式选择后允许非 18 位编号
    company = company_service.create_company(
        db, project, "境外公司", "FOREIGN-001", None, credit_code_type="other"
    )
    assert company.credit_code == "FOREIGN-001"
    assert company.credit_code_type == "other"


# ---------------------------------------------------------------- 证据口径


def _project_with_task(db, name: str, task_no: int) -> tuple[Project, AnalysisTask]:
    project = db.query(Project).filter(Project.project_name == name).first()
    if project is None:
        project = Project(project_name=name)
        db.add(project)
        db.commit()
    task = AnalysisTask(
        project_id=project.id,
        task_no=task_no,
        status="completed",
        analysis_config={"dimensions": ["text"], "thresholds": {}},
        company_snapshot={"companies": []},
        algorithm_version="test",
        progress={},
    )
    db.add(task)
    db.commit()
    return project, task


def _evidence(db, project, task) -> ComparisonEvidence:
    row = ComparisonEvidence(
        project_id=project.id,
        analysis_task_id=task.id,
        evidence_type="text",
        evidence_subtype="text_similarity",
        severity="medium",
        summary="重复证据",
        evidence_data={},
        review_status="pending",
        is_current=True,
    )
    db.add(row)
    db.commit()
    return row


def test_risk_list_only_shows_latest_task_evidence(db):
    project, first = _project_with_task(db, "重复分析项目", 1)
    _evidence(db, project, first)
    second = AnalysisTask(
        project_id=project.id,
        task_no=2,
        status="completed",
        analysis_config={"dimensions": ["text"], "thresholds": {}},
        company_snapshot={"companies": []},
        algorithm_version="test",
        progress={},
    )
    db.add(second)
    db.commit()
    latest = _evidence(db, project, second)

    result = risk_service.list_risks(db, project_id=project.id)
    assert result["total"] == 1
    assert result["items"][0]["id"] == latest.id

    # 显式查看历史时才包含旧任务证据
    history = risk_service.list_risks(db, project_id=project.id, include_history=True)
    assert history["total"] == 2


def test_tender_content_change_marks_task_stale(db):
    """招标正文变化（OCR 重识成功）后，旧任务必须提示基准过期；内容不变不提示。"""
    from app.models import AnalysisTask, DocumentPage, OriginalFile
    from app.models.enums import FileKind
    from app.services import analysis_service

    project = Project(project_name="招标基准项目")
    db.add(project)
    db.commit()
    tender = OriginalFile(
        project_id=project.id,
        kind=FileKind.TENDER.value,
        file_name="招标.pdf",
        file_type="pdf",
        storage_key="k",
        page_count=1,
        parse_status="success",
    )
    db.add(tender)
    db.commit()
    db.add(DocumentPage(original_file_id=tender.id, page_index=1, page_text="原招标正文"))
    db.commit()
    db.refresh(tender)

    signature = analysis_service._tender_content_signature(tender)
    task = AnalysisTask(
        project_id=project.id,
        task_no=1,
        status="completed",
        analysis_config={"dimensions": ["text"], "tender_signature": signature},
        company_snapshot={"companies": []},
        algorithm_version="test",
        progress={},
        tender_file_version_id=tender.id,
    )
    db.add(task)
    db.commit()

    assert analysis_service._tender_baseline_stale(db, task) is None

    page = db.query(DocumentPage).filter(DocumentPage.original_file_id == tender.id).one()
    page.page_text = "OCR 识别后的完整招标正文"
    db.commit()
    stale = analysis_service._tender_baseline_stale(db, task)
    assert stale is not None and stale["reason"] == "reparsed"


def test_quote_list_includes_detected_amounts(db):
    """报价清单页必须能看到最新任务自动识别的金额（只读），否则无法复核。"""
    from app.services import quote_service

    project = Project(project_name="报价清单项目")
    db.add(project)
    db.commit()
    company = Company(project_id=project.id, company_name="甲公司")
    db.add(company)
    db.commit()
    task = AnalysisTask(
        project_id=project.id,
        task_no=1,
        status="completed",
        analysis_config={
            "dimensions": ["price"],
            "price_snapshot": {
                "entries": {
                    company.id: {
                        "company_id": company.id,
                        "company_name": company.company_name,
                        "amount": 1_450_000.0,
                        "source_kind": "auto",
                        "source": {"file_name": "报价.pdf", "page_index": 3},
                        "needs_confirmation": False,
                    }
                }
            },
        },
        company_snapshot={"companies": []},
        algorithm_version="test",
        progress={},
    )
    db.add(task)
    db.commit()

    items = quote_service.list_quotes(db, project)
    assert items[0]["quote"] is None
    assert items[0]["detected"]["amount"] == 1_450_000
    assert items[0]["effective_source"] == "auto"


# ---------------------------------------------------------------- 令牌撤销


def test_logout_revokes_existing_token():
    from app.main import app

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200

        # 同一令牌在退出后必须失效
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
