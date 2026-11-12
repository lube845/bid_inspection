"""证据原文片段、上下文与高亮测试（比对分析页按需加载）。

覆盖：证据命中区间落回原文坐标、默认上下文读取、上下文按需扩展、
相似 / 差异高亮、来源信息（文件、页码、章节）以及比对分析页接口。
"""

from fastapi.testclient import TestClient

from app.analysis.sources import BlockRef
from app.analysis.text_similarity import build_text_evidences, find_text_matches
from app.main import app
from app.models import (
    AnalysisTask,
    Company,
    ComparisonEvidence,
    ComparisonPair,
    DocumentBlock,
    DocumentPage,
    OriginalFile,
    Project,
)
from app.models.enums import EvidenceType, RiskLevel, TaskStatus
from app.services import evidence_context, results_service
from app.utils.text import stable_hash

BODY_A = "我方将严格按照招标文件要求，结合项目实际情况科学组织施工，确保工程质量达到验收标准要求。"
BODY_B = "我方将严格按照招标文件要求，并结合项目实际情况科学组织施工，确保工程质量达到验收标准要求。"


def make_block(
    text: str,
    company_id: str = "c1",
    company_name: str = "甲公司",
    file_id: str = "f1",
    page: int = 1,
    block_id: str | None = None,
) -> BlockRef:
    return BlockRef(
        company_id=company_id,
        company_name=company_name,
        file_id=file_id,
        file_name=f"{company_name}.pdf",
        file_type="pdf",
        file_version=1,
        page_index=page,
        printed_page_number=None,
        block_id=block_id or f"b{stable_hash(company_id + text)}",
        block_index=0,
        content=text,
        normalized=text,
        block_type="paragraph",
    )


def _create_file_with_blocks(db, project, company, file_name, contents):
    """建一个只有一页的 PDF 文件，返回 (file, page, blocks)。"""
    file = OriginalFile(
        project_id=project.id,
        company_id=company.id,
        kind="bid",
        file_name=file_name,
        file_type="pdf",
        storage_key=f"{company.id}/{file_name}",
        file_size=sum(len(item) for item in contents),
        version=1,
        page_count=1,
        parse_status="success",
    )
    db.add(file)
    db.flush()
    page = DocumentPage(
        original_file_id=file.id,
        page_index=1,
        parse_status="success",
        extraction_method="native",
        page_text="",
    )
    db.add(page)
    db.flush()
    blocks = []
    for index, content in enumerate(contents):
        block = DocumentBlock(
            document_page_id=page.id,
            block_index=index,
            block_type="paragraph",
            content=content,
            normalized_content=content,
            start_offset=0,
            end_offset=len(content),
        )
        db.add(block)
        db.flush()
        blocks.append(block)
    return file, page, blocks


def _source(file, block, company, start, end, chapter="第三章 实施方案", section="3.1 质量保证"):
    return {
        "company_id": company.id,
        "company_name": company.company_name,
        "file_id": file.id,
        "file_name": file.file_name,
        "file_type": file.file_type,
        "file_version": file.version,
        "page_index": 1,
        "printed_page_number": "12",
        "block_id": block.id,
        "block_index": block.block_index,
        "start_offset": start,
        "end_offset": end,
        "snippet": block.content[start:end],
        "bbox": None,
        "locator_precision": "block",
        "extraction_method": "native",
        "ranges": [
            {
                "block_id": block.id,
                "page_index": 1,
                "file_id": file.id,
                "start_offset": start,
                "end_offset": end,
            }
        ],
        "chapter": chapter,
        "section": section,
        "page_start": 1,
        "page_end": 1,
    }


def _prepare_evidence(db, body_a=BODY_A, body_b=BODY_B):
    """搭一套完整的项目 / 企业 / 文件 / 证据数据，返回 (evidence, file_a)。"""
    project = Project(project_name="上下文测试项目", project_code="CTX-001")
    db.add(project)
    db.flush()
    company_a = Company(project_id=project.id, company_name="甲公司")
    company_b = Company(project_id=project.id, company_name="乙公司")
    db.add_all([company_a, company_b])
    db.flush()

    prefix = "投标文件封面说明，前置内容用于验证上下文读取。" * 5
    suffix = "后置内容用于验证上下文读取与扩展，投标文件正文到此结束。" * 6
    file_a, _, blocks_a = _create_file_with_blocks(
        db, project, company_a, "甲公司投标文件.pdf", [prefix + body_a + suffix]
    )
    file_b, _, blocks_b = _create_file_with_blocks(
        db, project, company_b, "乙公司投标文件.pdf", [prefix + body_b + suffix]
    )

    task = AnalysisTask(
        project_id=project.id,
        task_no=1,
        status=TaskStatus.COMPLETED.value,
        analysis_config={},
        company_snapshot={
            "companies": [
                {"company_id": company_a.id, "company_name": company_a.company_name},
                {"company_id": company_b.id, "company_name": company_b.company_name},
            ]
        },
        algorithm_version="v1.0.0-minhash-lsh",
    )
    db.add(task)
    db.flush()
    pair = ComparisonPair(
        analysis_task_id=task.id,
        company_a_id=company_a.id,
        company_b_id=company_b.id,
        text_risk=RiskLevel.MEDIUM.value,
        overall_risk=RiskLevel.MEDIUM.value,
        status="completed",
    )
    db.add(pair)
    db.flush()

    block_a = blocks_a[0]
    block_b = blocks_b[0]
    start_a = len(prefix)
    end_a = start_a + len(body_a)
    start_b = len(prefix)
    end_b = start_b + len(body_b)
    source_a = _source(file_a, block_a, company_a, start_a, end_a)
    source_b = _source(file_b, block_b, company_b, start_b, end_b)

    evidence = ComparisonEvidence(
        project_id=project.id,
        analysis_task_id=task.id,
        comparison_pair_id=pair.id,
        evidence_type=EvidenceType.TEXT.value,
        evidence_subtype="text_similarity",
        severity=RiskLevel.MEDIUM.value,
        summary="甲公司与乙公司存在相似文本",
        evidence_data={
            "similarity": 0.9,
            "similarity_percent": 90.0,
            "matched_chars": len(body_a),
            "match_intervals_a": [
                {
                    "block_id": block_a.id,
                    "page_index": 1,
                    "start": start_a,
                    "end": end_a,
                }
            ],
            "match_intervals_b": [
                {
                    "block_id": block_b.id,
                    "page_index": 1,
                    "start": start_b,
                    "end": end_b - 1,
                }
            ],
            "match_interval_count": 1,
        },
        source_a=source_a,
        source_b=source_b,
        algorithm_version="v1.0.0-minhash-lsh",
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence, file_a, pair


def _kinds(payload):
    return [segment["kind"] for segment in payload["segments"]]


def _text_of(payload, kind):
    return "".join(
        segment["text"] for segment in payload["segments"] if segment["kind"] == kind
    )


# --- 相似 / 差异高亮 -------------------------------------------------------


def test_similar_text_is_marked_and_difference_is_highlighted(db):
    evidence, _file, _pair = _prepare_evidence(db)
    sides = results_service.evidence_detail(db, evidence)["sides"]

    assert sides["a"]["segments"], "证据必须给出可展示的原文片段"
    assert "match" in _kinds(sides["a"])
    # A 侧命中区间完整，B 侧少一个字，这一个字必须标成差异
    assert "diff" in _kinds(sides["b"])
    assert "diff" not in _kinds(sides["a"])
    assert sides["diff"]["b_chars"] >= 1
    assert "橙色" in sides["diff"]["summary"]


def test_match_segment_contains_the_hit_text_only(db):
    evidence, _file, _pair = _prepare_evidence(db)
    sides = results_service.evidence_detail(db, evidence)["sides"]
    matched = _text_of(sides["a"], "match")
    assert matched == BODY_A
    # 上下文不与命中片段混在一起
    assert BODY_A not in _text_of(sides["a"], "context")


# --- 上下文读取 ------------------------------------------------------------


def test_default_context_is_limited_and_keeps_source_info(db):
    evidence, _file, _pair = _prepare_evidence(db)
    sides = results_service.evidence_detail(db, evidence)["sides"]
    side = sides["a"]

    assert side["context_before"] <= 100
    assert side["context_after"] <= 100
    assert side["context_before"] > 0
    assert side["context_after"] > 0
    assert side["file_name"] == "甲公司投标文件.pdf"
    assert side["page_start"] == 1
    assert side["chapter"] == "第三章 实施方案"
    assert side["section"] == "3.1 质量保证"
    assert side["has_more_after"] is True


def test_context_expansion_returns_more_text_only_for_requested_side(db):
    evidence, _file, _pair = _prepare_evidence(db)
    payload = evidence_context.build_side_context(
        db, evidence, "a", before=300, after=300
    )["side_payload"]
    assert payload["context_before"] > 100
    assert payload["context_after"] > 100
    # 命中片段本身不随上下文窗口变化
    assert _text_of(payload, "match") == BODY_A


def test_context_is_bounded_by_document_start(db):
    """命中片段靠近文档开头时，只返回实际可获得的前文。"""
    evidence, _file, _pair = _prepare_evidence(db)
    payload = evidence_context.build_side_context(
        db, evidence, "a", before=2000, after=0
    )["side_payload"]
    assert payload["context_before"] < 2000
    assert payload["has_more_before"] is False


def test_context_request_is_capped_by_server(db):
    evidence, _file, _pair = _prepare_evidence(db)
    payload = evidence_context.build_side_context(
        db, evidence, "a", before=999999, after=0
    )
    assert payload["before_chars"] <= evidence_context.MAX_CONTEXT_CHARS


# --- 算法侧：相似区间落回原文坐标 ------------------------------------------


def test_distant_fragments_become_independent_evidences():
    """同一组候选里的多处不连续相似片段必须拆成多条独立证据。"""
    fragment_1 = "质量管理体系包含三道检查制度，进场材料必须见证取样送检。" * 2
    fragment_2 = "隐蔽工程验收合格后方可进入下一道工序，记录完整归档。" * 2
    # 两侧中间段内容完全不同，保证只有两处片段相似
    filler_a = "甲" * 120
    filler_b = "乙" * 120
    blocks_a = [make_block(f"{fragment_1}{filler_a}{fragment_2}", "c1", "甲公司", "f1")]
    blocks_b = [make_block(f"{fragment_1}{filler_b}{fragment_2}", "c2", "乙公司", "f2")]

    matches = find_text_matches(blocks_a, blocks_b)
    drafts = build_text_evidences(matches, None)
    assert len(drafts) >= 2, "相距较远的相似片段不能拼成一条证据"
    for draft in drafts:
        assert draft.source_a["ranges"]
        assert draft.source_b["ranges"]
        assert draft.evidence_data["match_intervals_a"]


def test_evidence_group_marks_position_inside_same_candidate():
    """同一组候选下的多条证据要标出「第 N 处 / 共 M 处」。"""
    items = [
        {"id": "e1", "group_key": "chunk-a|chunk-b"},
        {"id": "e2", "group_key": "chunk-a|chunk-b"},
        {"id": "e3", "group_key": None},
    ]
    positioned = results_service._with_group_positions(items)
    assert positioned[0]["group_index"] == 1
    assert positioned[0]["group_size"] == 2
    assert positioned[1]["group_index"] == 2
    assert positioned[1]["group_size"] == 2
    assert positioned[2]["group_index"] is None
    assert positioned[2]["group_size"] is None


def test_match_intervals_are_saved_in_original_coordinates():
    text = BODY_A * 4
    blocks_a = [make_block(text, "c1", "甲公司", "f1")]
    blocks_b = [make_block(text, "c2", "乙公司", "f2")]
    matches = find_text_matches(blocks_a, blocks_b)
    drafts = build_text_evidences(matches, None)
    assert drafts
    data = drafts[0].evidence_data
    assert data["match_intervals_a"]
    assert data["match_intervals_b"]
    interval = data["match_intervals_a"][0]
    assert interval["block_id"]
    assert interval["end"] > interval["start"]
    assert data["match_interval_count"] == len(data["match_intervals_a"])


# --- 接口 ------------------------------------------------------------------


def test_pair_evidence_endpoint_returns_context_without_full_document(db):
    evidence, _file, pair = _prepare_evidence(db)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        listed = client.get(
            f"/api/v1/pairs/{pair.id}/evidences?evidence_type=text", headers=headers
        )
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["total"] == 1
        item = payload["items"][0]
        assert item["id"] == evidence.id
        assert item["sides"]["a"]["segments"]
        # 首屏只给片段，不返回整份原文
        assert "page_text" not in item["sides"]["a"]

        detail = client.get(f"/api/v1/evidences/{evidence.id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["sides"]["a"]["segments"]

        context = client.get(
            f"/api/v1/evidences/{evidence.id}/context?side=b&before=300&after=100",
            headers=headers,
        )
        assert context.status_code == 200
        assert context.json()["side"] == "b"
        assert context.json()["side_payload"]["context_before"] > 100

        bad_side = client.get(
            f"/api/v1/evidences/{evidence.id}/context?side=x", headers=headers
        )
        assert bad_side.status_code == 422

        missing = client.get("/api/v1/evidences/not-a-real-id", headers=headers)
        assert missing.status_code == 404


def test_file_original_endpoint_returns_access_address(db):
    evidence, file_a, _pair = _prepare_evidence(db)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get(f"/api/v1/files/{file_a.id}/original", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["file_id"] == file_a.id
        assert payload["url"] == f"/files/{file_a.id}/content"
        assert payload["file_version"] == file_a.version
        assert payload["pages"][0]["page_index"] == 1
