"""文档解析兼容性测试。"""

import pytest

from app.services.parser import ParseError, parse_document


def test_wps_ooxml_is_read_as_docx(tmp_path):
    from docx import Document

    document = Document()
    document.add_paragraph("项目实施方案应当结合实际情况组织施工")
    path = tmp_path / "投标文件.wps"
    document.save(path)

    parsed = parse_document(path, "wps")
    assert parsed.pages
    assert "项目实施方案" in parsed.pages[0].page_text


def test_legacy_doc_is_rejected_with_clear_message(tmp_path):
    path = tmp_path / "legacy.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(ParseError, match="DOC"):
        parse_document(path, "doc")
