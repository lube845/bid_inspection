"""P0 缺陷回归测试.

覆盖这五类曾经让结论不可信的问题：

1. 解析器字段名写错，含空白/扫描页的 PDF 整份解析失败；
2. 真实 PDF/DOCX 上章节标题识别完全失效，证据的章节字段永远为空；
3. 编号条目被误判成标题后从分析中丢弃（漏报）；
4. 同一线索在一条任务内生成多条重复证据，把风险等级虚高；
5. 「亿元」「纯大写金额」无法识别，总报价取错分项金额。
"""

from pathlib import Path

from app.analysis.chunking import build_chunks
from app.analysis.price import (
    extract_quote,
    parse_upper_amount,
)
from app.analysis.sources import BlockRef
from app.analysis.text_similarity import (
    TextIndex,
    build_text_evidences,
    match_chunks,
)
from app.analysis.thresholds import text_thresholds
from app.services.parser import ParsedPage, parse_document
from app.utils.text import stable_hash


def make_block(
    text: str,
    company_id: str = "c1",
    company_name: str = "甲公司",
    file_id: str = "f1",
    file_name: str = "技术标.pdf",
    block_id: str | None = None,
    page: int = 1,
) -> BlockRef:
    return BlockRef(
        company_id=company_id,
        company_name=company_name,
        file_id=file_id,
        file_name=file_name,
        file_type="pdf",
        file_version=1,
        page_index=page,
        printed_page_number=None,
        block_id=block_id or f"b{stable_hash(company_id + file_id + text)}",
        block_index=0,
        content=text,
        normalized=text,
        block_type="paragraph",
    )


# --- 1. 解析器：含空白页的 PDF 不能整份失败 ---------------------------------


def _write_pdf_with_blank_page(path: Path) -> None:
    """构造 3 页 PDF：第 1、3 页有文本，第 2 页是空白页。"""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 6 0 R >> >> /Contents 7 0 R >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 6 0 R >> >> /Contents 8 0 R >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 6 0 R >> >> /Contents 9 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        "<< /Length 55 >>\nstream\nBT /F1 12 Tf 50 700 Td (Implementation Plan) Tj ET\nendstream",
        "<< /Length 20 >>\nstream\nBT /F1 12 Tf 50 700 Td () Tj ET\nendstream",
        "<< /Length 56 >>\nstream\nBT /F1 12 Tf 50 700 Td (Quality Management Measures) Tj ET\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def test_parsed_page_accepts_width_and_height():
    """回归：ParsedPage 的字段是 width/height，不是 page_width/page_height。"""
    page = ParsedPage(page_index=1, page_text="", width=595.0, height=842.0)
    assert page.width == 595.0
    assert page.height == 842.0


def test_pdf_with_blank_page_is_parsed_as_partial_not_failed(tmp_path):
    """回归：第 2 页没有文本时，整份 PDF 仍然解析成功，空白页标记为 empty。"""
    path = tmp_path / "blank.pdf"
    _write_pdf_with_blank_page(path)

    document = parse_document(path, "pdf")

    assert len(document.pages) == 3
    assert document.pages[0].page_text.strip()
    assert document.pages[1].parse_status == "empty"
    assert document.pages[1].page_text == ""
    assert document.pages[2].page_text.strip()
    # 页面尺寸也要保存下来
    assert document.pages[0].width == 595.0


# --- 2 & 3. 分块：章节可识别、标题不进正文、编号条目不丢 ---------------------


def test_chapter_and_section_are_detected_in_realistic_pdf_page():
    """回归：整页 PDF 文本（无空行）也要能识别章节与小节。"""
    page = (
        "第三章 项目实施方案\n"
        "3.1 项目总体方案\n"
        "项目总体方案应当结合本项目实际情况，建立完善的项目实施管理体系。\n"
        "3.2 项目组织架构\n"
        "为保障项目顺利实施，我公司拟成立项目管理小组。\n"
    )
    chunks = build_chunks([make_block(page)], text_thresholds(None))

    assert chunks
    assert chunks[0].chapter == "第三章 项目实施方案"
    assert "3.1 项目总体方案" in chunks[0].section
    # 标题只作为结构元数据，不进入正文
    assert "第三章" not in chunks[0].normalized
    assert "3.1项目总体方案" not in chunks[0].normalized
    # 正文没有丢
    assert "项目总体方案应当结合本项目实际情况" in chunks[0].normalized


def test_numbered_body_items_are_kept_in_analysis():
    """回归：正文里的编号条目（1. / （一））不能被当成标题丢掉。"""
    text = (
        "技术方案\n"
        "\n"
        "1. 本公司针对本项目建立完善的质量管理体系，配备专职质量管理人员。\n"
        "\n"
        "2. 所有进场材料进场前必须进行检验并留存合格证明文件。\n"
        "\n"
        "（一）严格执行国家相关规范标准组织施工。\n"
    )
    chunks = build_chunks([make_block(text)], text_thresholds(None))

    assert chunks
    normalized = "".join(chunk.normalized for chunk in chunks)
    assert "本公司针对本项目建立完善的质量管理体系" in normalized
    assert "所有进场材料进场前必须进行检验" in normalized
    assert "严格执行国家相关规范标准组织施工" in normalized


def test_content_type_is_derived_from_chapter():
    """回归：章节识别后内容类型不再恒为 general。"""
    page = (
        "第三章 技术方案\n"
        "我方将采用先进的施工工艺组织施工，确保工程质量达到验收标准要求。\n"
    )
    chunks = build_chunks([make_block(page)], text_thresholds(None))
    assert chunks
    assert chunks[0].content_type == "technical"


# --- 4. 证据去重 ------------------------------------------------------------


def test_duplicate_matches_are_merged_into_single_evidence():
    """回归：同一段文字在文件里出现多次时，只产出 1 条证据。"""
    body = (
        "本项目实施方案包括技术方案质量措施安全文明施工进度控制合同管理等具体内容。"
        * 6
    )
    text = f"一、项目实施方案\n{body}\n二、质量管理措施\n{body}"
    rules = text_thresholds(None)

    chunks_a = build_chunks(
        [make_block(text, "c1", "甲公司", "f1")], rules
    )
    chunks_b = build_chunks(
        [make_block(text, "c2", "乙公司", "f2")], rules
    )
    index = TextIndex(rules)
    index.add_chunks(chunks_a + chunks_b)
    candidates = index.candidates_between(chunks_a, chunks_b)
    matches, stats = match_chunks(candidates, None, thresholds=None)

    # 局部对齐会命中多处，但证据必须归并
    assert stats["local_match_count"] > 1
    drafts = build_text_evidences(matches, None, thresholds=None)
    assert len(drafts) == 1
    assert drafts[0].evidence_data["merged_duplicate_count"] >= 1


# --- 5. 价格解析 ------------------------------------------------------------


def test_uppercase_amount_is_parsed():
    assert parse_upper_amount("壹仟万元整") == 10_000_000
    assert parse_upper_amount("人民币壹佰伍拾万元整") == 1_500_000
    assert parse_upper_amount("壹亿贰仟万整") == 120_000_000
    assert parse_upper_amount("贰仟零伍佰元整") == 2_500
    assert parse_upper_amount("壹佰贰拾叁万肆仟伍佰陆拾柒元捌角玖分") == 1_234_567.89


def test_total_quote_supports_yi_yuan_and_keeps_best_candidate():
    """回归：支持「亿元」，且分项报价不能盖过真正的投标总报价。"""
    blocks = [
        make_block(
            "分项报价表\n报价金额：120000元\n设备采购费\n报价金额：80000元",
            file_name="报价.txt",
        ),
        make_block(
            "开标一览表\n投标总报价：1.5亿元\n大写：人民币壹亿伍仟万元整",
            file_name="报价.txt",
        ),
    ]
    quote = extract_quote("c1", "甲公司", blocks)

    assert quote is not None
    assert quote.amount == 150_000_000


def test_total_quote_falls_back_to_uppercase_amount():
    blocks = [make_block("投标总报价：壹佰伍拾万元整", file_name="报价.txt")]
    quote = extract_quote("c1", "甲公司", blocks)

    assert quote is not None
    assert quote.amount == 1_500_000
    assert quote.upper == "壹佰伍拾万元整"
