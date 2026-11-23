"""第三轮 QA 报告（prd_kimi_test3.md）缺陷的回归测试。

覆盖：
- 康熙部首 / 兼容汉字折叠：PDF 人员重复不再整份漏报；
- 「实施工程师」等岗位进入人员词表（王强漏报）；
- 报价候选矛盾的文案不再出现「多个合计金额（ 元）」或缺金额；
- 页面详情接口按文本块分页（大文件不再一次返回整页）；
- 证据来源继承页面的真实提取方式（OCR 页不再标成 native）；
- 导出时区与页面一致（配置 display_timezone）；
- 用户可自助修改密码，初始口令不能改回自身；
- 项目彻底删除（purge）清空文件、任务、证据与企业；
- DOC 不再出现在允许上传的格式里。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.analysis.facts import extract_facts
from app.analysis.line_items import extract_line_items
from app.analysis.personnel import extract_people
from app.analysis.price import Quote, analyze_quote_quality, extract_quote
from app.analysis.sources import BlockRef
from app.models import AnalysisTask, Company, ComparisonEvidence, Project
from app.utils.text import fold, fold_for_match
from app.models.enums import FileKind


def _block(text: str, **overrides) -> BlockRef:
    data = dict(
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
    data.update(overrides)
    return BlockRef(**data)


# ---------------------------------------------------------------- 字符折叠


def test_fold_maps_kangxi_radicals_and_keeps_1to1_offsets():
    text = "项⽬经理：张明"
    folded, offsets = fold_for_match(text)
    # 康熙部首折叠成标准汉字；全角冒号被 NFKC 统一为半角（同样是 1:1）
    assert folded == "项目经理:张明"
    assert len(folded) == len(text)
    assert offsets == list(range(len(text)))
    # 标点、括号与空白都保留（只折叠码位，不清洗文本）
    assert fold("（ ⼈ ）") == "( 人 )"


@pytest.mark.parametrize(
    "text",
    [
        "项⽬经理：张明\n技术负责⼈：李华\n实施⼯程师：王强",
        "项⽬经理：张明 技术负责⼈：李华 实施⼯程师：王强",
    ],
)
def test_personnel_extraction_survives_kangxi_radicals(text):
    """QA 报告第 1 条：PDF 康熙部首导致人员重复全部漏报。"""
    people = extract_people([_block(text)])
    assert {p.name for p in people} == {"张明", "李华", "王强"}


def test_personnel_offset_maps_back_to_original_text():
    block = _block("项⽬经理：张明")
    people = extract_people([block])
    assert len(people) == 1
    occurrence = people[0]
    assert block.content[occurrence.start:occurrence.end] == "张明"


def test_line_item_and_facts_use_folded_text():
    block = _block("⼟⽅开挖\tm3\t100\t12.5")
    items = extract_line_items([block])
    (item,) = items.values()
    assert item.unit_price == 12.5
    assert block.content[item.start:item.end] == "⼟⽅开挖"

    facts = extract_facts([_block("项⽬经理：张伟 工期：90日历天")])
    assert {f.kind for f in facts} >= {"role_name", "duration"}


# ---------------------------------------------------------------- 报价文案


def test_conflicting_totals_never_print_empty_candidate_list():
    """回归：曾出现「自动识别到多个合计金额（ 元）」的空括号。"""
    quote = Quote(
        company_id="c1",
        company_name="甲公司",
        amount=1_400_000.0,
        raw="",
        upper=None,
        block=None,
        start=0,
        end=0,
        alternatives=[1_400_000.0],
        needs_confirmation=True,
    )
    (finding,) = analyze_quote_quality([quote])
    assert "多个合计金额（" not in finding.summary
    assert "元）" not in finding.summary
    assert "1,400,000.00" in finding.summary


def test_single_candidate_is_not_called_multiple():
    quote = Quote(
        company_id="c1",
        company_name="甲公司",
        amount=1_450_000.0,
        raw="",
        upper=None,
        block=None,
        start=0,
        end=0,
        alternatives=[1_450_000.0, 1_400_000.0],
        needs_confirmation=True,
    )
    (finding,) = analyze_quote_quality([quote])
    assert "1,450,000.00" in finding.summary and "1,400,000.00" in finding.summary


def test_quote_amounts_survive_kangxi_radicals():
    text = "投标总报价：1,400,000.00 元"
    quote = extract_quote("c1", "甲公司", [_block(text)])
    assert quote is not None and quote.amount == 1_400_000


# ---------------------------------------------------------------- 来源字段


def test_block_source_keeps_ocr_extraction_method():
    """QA 报告第 4 条：扫描页的报价来源被写成 native。"""
    block = _block("投标总报价：1,200,000 元", extraction_method="ocr")
    quote = extract_quote("c1", "甲公司", [block])
    assert quote is not None
    assert quote.source["extraction_method"] == "ocr"


def test_block_defaults_to_native():
    block = _block("投标总报价：1,200,000 元")
    assert block.source()["extraction_method"] == "native"


# ---------------------------------------------------------------- 时区


def test_export_time_is_converted_to_display_timezone():
    from datetime import datetime, timezone

    from app.services.results_service import _format_display_time

    # 2026-11-18 02:30 UTC = 10:30 东八区
    assert _format_display_time(datetime(2026, 11, 18, 2, 30, tzinfo=timezone.utc)) == "2026-11-18 10:30"
    # naive 时间按 UTC 处理，同样换算
    assert _format_display_time(datetime(2026, 11, 18, 2, 30)) == "2026-11-18 10:30"
    assert _format_display_time(None) == ""


# ---------------------------------------------------------------- 接口


def _client():
    from app.main import app

    return TestClient(app)


def _login(client):
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_change_password_requires_current_password_and_revokes_token():
    with _client() as client:
        headers = _login(client)

        bad = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "wrong", "new_password": "NEW-password-1"},
            headers=headers,
        )
        assert bad.status_code == 400

        # 等于部署初始口令同样拒绝
        same = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "admin123", "new_password": "admin123"},
            headers=headers,
        )
        assert same.status_code == 400

        ok = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "admin123", "new_password": "NEW-password-1"},
            headers=headers,
        )
        assert ok.status_code == 200
        # 改密后旧令牌立即失效
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401

        # 新密码可登录
        relogin = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "NEW-password-1"},
        )
        assert relogin.status_code == 200


def test_page_detail_paginates_blocks(db):
    from app.models import DocumentBlock, DocumentPage, OriginalFile

    project = Project(project_name="大文件项目")
    db.add(project)
    db.commit()
    file = OriginalFile(
        project_id=project.id,
        kind=FileKind.BID.value,
        file_name="big.txt",
        file_type="txt",
        storage_key="k",
        page_count=1,
        parse_status="success",
    )
    db.add(file)
    db.commit()
    page = DocumentPage(original_file_id=file.id, page_index=1, page_text="x" * 500)
    db.add(page)
    db.flush()
    for index in range(120):
        db.add(
            DocumentBlock(
                document_page_id=page.id,
                block_index=index,
                block_type="paragraph",
                content=f"块 {index}",
                normalized_content=f"块{index}",
            )
        )
    db.commit()

    with _client() as client:
        headers = _login(client)
        first = client.get(
            f"/api/v1/files/{file.id}/pages/1?block_offset=0&block_limit=50",
            headers=headers,
        ).json()["page"]
        assert len(first["blocks"]) == 50
        assert first["block_total"] == 120
        assert first["has_more_blocks"] is True

        last = client.get(
            f"/api/v1/files/{file.id}/pages/1?block_offset=100&block_limit=50",
            headers=headers,
        ).json()["page"]
        assert len(last["blocks"]) == 20
        assert last["has_more_blocks"] is False


def test_purge_project_removes_data_but_keeps_audit_log(db):
    project = Project(project_name="待彻底删除项目")
    db.add(project)
    db.commit()
    company = Company(project_id=project.id, company_name="甲公司")
    db.add(company)
    db.commit()
    task = AnalysisTask(
        project_id=project.id,
        task_no=1,
        status="completed",
        analysis_config={"dimensions": ["text"], "thresholds": {}},
        company_snapshot={"companies": []},
        algorithm_version="test",
        progress={},
    )
    db.add(task)
    db.commit()
    evidence = ComparisonEvidence(
        project_id=project.id,
        analysis_task_id=task.id,
        evidence_type="text",
        evidence_subtype="text_similarity",
        severity="medium",
        summary="证据",
        evidence_data={},
        review_status="pending",
        is_current=True,
    )
    db.add(evidence)
    db.commit()
    project_id, company_id, task_id, evidence_id = (
        project.id,
        company.id,
        task.id,
        evidence.id,
    )

    with _client() as client:
        headers = _login(client)
        response = client.delete(f"/api/v1/projects/{project_id}?purge=true", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["purged"] is True and body["companies"] == 1

    db.expire_all()
    assert db.get(Project, project_id) is None
    assert db.get(Company, company_id) is None
    assert db.get(AnalysisTask, task_id) is None
    assert db.get(ComparisonEvidence, evidence_id) is None

    from app.models import AuditLog

    assert (
        db.query(AuditLog).filter(AuditLog.action == "project.purge").count() == 1
    )


def test_doc_is_rejected_at_upload_time():
    from app.core.config import settings

    assert "doc" not in settings.allowed_extensions
