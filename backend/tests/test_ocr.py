"""OCR 识别测试（百度智能云 OCR 接入）。

覆盖：识别器构造、access_token 获取与缓存、请求体拼装（PDF 按页 / 图片）、
响应解析、失败兜底，以及「OCR 识别」规则维度的版本管理、凭据脱敏与校验。
"""

import base64
import io
import json
import urllib.parse

import pytest

from app.analysis.thresholds import text_thresholds  # noqa: F401  (保持测试依赖顺序)
from app.models import DetectionRuleVersion
from app.models.enums import ExtractionMethod, RuleStatus
from app.services import ocr, rule_service
from app.services.parser import parse_document


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_urlopen(monkeypatch, handler):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(ocr.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_provider_is_disabled_by_default():
    provider = ocr.build_ocr_provider(None)
    assert isinstance(provider, ocr.NoopOCRProvider)
    assert provider.extract_text("x.pdf", 1) is None


def test_provider_requires_credentials():
    provider = ocr.build_ocr_provider(
        {"ocr_enabled": True, "ocr_provider": "baidu", "baidu_api_key": "", "baidu_secret_key": ""}
    )
    # 凭据不完整时退回空实现，解析流程不中断
    assert isinstance(provider, ocr.NoopOCRProvider)


def test_unknown_provider_falls_back_to_noop():
    provider = ocr.build_ocr_provider({"ocr_enabled": True, "ocr_provider": "unknown"})
    assert isinstance(provider, ocr.NoopOCRProvider)


def test_access_token_is_fetched_and_cached(monkeypatch):
    ocr.clear_token_cache()
    calls = _patch_urlopen(
        monkeypatch,
        lambda request: _FakeResponse(
            json.dumps({"access_token": "token-123", "expires_in": 3600}).encode()
        ),
    )
    token = ocr.fetch_access_token("ak", "sk")
    assert token == "token-123"
    assert len(calls) == 1
    # 第二次命中缓存，不再请求
    assert ocr.fetch_access_token("ak", "sk") == "token-123"
    assert len(calls) == 1


def test_access_token_error_is_reported(monkeypatch):
    ocr.clear_token_cache()

    def handler(request):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, None
        )

    _patch_urlopen(monkeypatch, handler)
    with pytest.raises(ocr.OCRError):
        ocr.fetch_access_token("ak", "sk")


def test_pdf_uses_pdf_file_and_page_number(monkeypatch, tmp_path):
    """PDF 页以 pdf_file 上传；拆分后每个文件只有一页，页码固定为 1。"""
    ocr.clear_token_cache()
    path = _write_pdf(tmp_path / "sample.pdf", 2)
    seen = {}

    def handler(request):
        url = request.full_url
        if "oauth" in url:
            return _FakeResponse(json.dumps({"access_token": "tk", "expires_in": 60}).encode())
        body = urllib.parse.parse_qs(request.data.decode())
        seen.update({key: value[0] for key, value in body.items()})
        return _FakeResponse(
            json.dumps({"words_result": [{"words": "第一条"}, {"words": "第二条"}]}).encode()
        )

    _patch_urlopen(monkeypatch, handler)
    provider = ocr.BaiduOCRProvider("ak", "sk")
    text = provider.extract_text(path, 2)
    assert text == "第一条\n第二条"
    assert "pdf_file" in seen
    assert seen["pdf_file_num"] == "1"
    assert "image" not in seen
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(base64.b64decode(seen["pdf_file"])))
    assert len(reader.pages) == 1


def test_image_uses_image_field(monkeypatch, tmp_path):
    ocr.clear_token_cache()
    path = tmp_path / "page.png"
    path.write_bytes(b"\x89PNG fake")
    seen = {}

    def handler(request):
        if "oauth" in request.full_url:
            return _FakeResponse(json.dumps({"access_token": "tk", "expires_in": 60}).encode())
        body = urllib.parse.parse_qs(request.data.decode())
        seen.update({key: value[0] for key, value in body.items()})
        return _FakeResponse(json.dumps({"words_result": [{"words": "图片文字"}]}).encode())

    _patch_urlopen(monkeypatch, handler)
    provider = ocr.BaiduOCRProvider("ak", "sk")
    assert provider.extract_text(path, 1) == "图片文字"
    assert "image" in seen
    assert "pdf_file" not in seen


def test_recognition_failure_returns_none(monkeypatch, tmp_path):
    ocr.clear_token_cache()
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4 fake")

    def handler(request):
        if "oauth" in request.full_url:
            return _FakeResponse(json.dumps({"access_token": "tk", "expires_in": 60}).encode())
        return _FakeResponse(
            json.dumps({"error_code": 18, "error_msg": "图片尺寸超限"}).encode()
        )

    _patch_urlopen(monkeypatch, handler)
    provider = ocr.BaiduOCRProvider("ak", "sk")
    # 识别失败不抛出，页面会被标记为未识别
    assert provider.extract_text(path, 1) is None


def test_oversized_broken_file_is_not_recognized(monkeypatch, tmp_path):
    """超限文件会先尝试拆分；文件本身损坏时只标记未识别，不抛异常。"""
    path = tmp_path / "big.pdf"
    path.write_bytes(b"0" * (9 * 1024 * 1024))
    provider = ocr.BaiduOCRProvider("ak", "sk")
    assert provider.extract_text(path, 1) is None


def test_blank_pdf_page_uses_ocr_result(tmp_path):
    """没有文本层的 PDF 页应走 OCR，识别结果写入页面文本。"""
    from pypdf import PdfWriter

    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with open(path, "wb") as handle:
        writer.write(handle)

    class _StubProvider(ocr.OCRProvider):
        name = "测试 OCR"

        def extract_text(self, file_path, page_index):
            return f"第 {page_index} 页扫描文字"

    parsed = parse_document(path, "pdf", ocr_provider=_StubProvider())
    assert parsed.pages
    page = parsed.pages[0]
    assert page.extraction_method == ExtractionMethod.OCR.value
    assert page.page_text == "第 1 页扫描文字"
    assert page.blocks and page.blocks[0].content


def test_pdf_pages_are_recognized_in_one_batch(tmp_path):
    """解析器把没有文本层的页一次性交给识别器批量处理。"""
    from pypdf import PdfWriter

    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=595, height=842)
    with open(path, "wb") as handle:
        writer.write(handle)

    calls = []

    class _BatchProvider(ocr.OCRProvider):
        def extract_text(self, file_path, page_index):  # pragma: no cover - 不应被调用
            raise AssertionError("批量识别时不应逐页调用")

        def extract_pages(self, file_path, page_indexes):
            calls.append(list(page_indexes))
            return {index: f"第 {index} 页" for index in page_indexes}

    parsed = parse_document(path, "pdf", ocr_provider=_BatchProvider())
    assert calls == [[1, 2, 3]]
    assert [page.page_index for page in parsed.pages] == [1, 2, 3]
    assert all(page.extraction_method == ExtractionMethod.OCR.value for page in parsed.pages)


# --- 大文件拆分 -------------------------------------------------------------


def _write_pdf(path, pages: int = 3):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def test_split_pdf_pages_produces_single_page_files(tmp_path):
    path = _write_pdf(tmp_path / "doc.pdf", 3)
    parts = ocr.split_pdf_pages(path, [1, 2, 3], budget=10 * 1024 * 1024)
    assert len(parts) == 3
    assert [part.pages for part in parts] == [{1: 1}, {2: 1}, {3: 1}]
    # 每个拆分文件都只含一页
    from pypdf import PdfReader

    for part in parts:
        reader = PdfReader(io.BytesIO(part.data))
        assert len(reader.pages) == 1


def test_pdf_is_uploaded_page_by_page(monkeypatch, tmp_path):
    """PDF 始终按页拆开上传：不会为每一页重复传输整份文件。"""
    path = _write_pdf(tmp_path / "doc.pdf", 2)
    uploads = []

    def handler(request):
        if "oauth" in request.full_url:
            return _FakeResponse(json.dumps({"access_token": "tk", "expires_in": 60}).encode())
        body = urllib.parse.parse_qs(request.data.decode())
        uploads.append({key: value[0] for key, value in body.items()})
        return _FakeResponse(json.dumps({"words_result": [{"words": "文字"}]}).encode())

    _patch_urlopen(monkeypatch, handler)
    provider = ocr.BaiduOCRProvider("ak", "sk")
    results = provider.extract_pages(path, [1, 2])
    assert results == {1: "文字", 2: "文字"}
    assert len(uploads) == 2
    from pypdf import PdfReader

    for upload in uploads:
        # 每个上传的文件只含一页
        assert upload["pdf_file_num"] == "1"
        reader = PdfReader(io.BytesIO(base64.b64decode(upload["pdf_file"])))
        assert len(reader.pages) == 1
    # 上传总量约等于原文件大小，而不是原文件的页数倍
    total = sum(len(base64.b64decode(upload["pdf_file"])) for upload in uploads)
    assert total < path.stat().st_size * 2


def test_oversized_pdf_is_split_per_page(monkeypatch, tmp_path):
    path = _write_pdf(tmp_path / "doc.pdf", 3)
    uploads = []

    def handler(request):
        if "oauth" in request.full_url:
            return _FakeResponse(json.dumps({"access_token": "tk", "expires_in": 60}).encode())
        body = urllib.parse.parse_qs(request.data.decode())
        uploads.append({key: value[0] for key, value in body.items()})
        return _FakeResponse(json.dumps({"words_result": [{"words": "文字"}]}).encode())

    _patch_urlopen(monkeypatch, handler)
    # 把单次体积上限压到很小，强制走拆分分支
    monkeypatch.setattr(
        ocr.BaiduOCRProvider, "budget_bytes", property(lambda self: 1)
    )
    provider = ocr.BaiduOCRProvider("ak", "sk")
    results = provider.extract_pages(path, [1, 2, 3])
    assert results == {1: "文字", 2: "文字", 3: "文字"}
    assert provider.last_split is True
    # 每一页单独上传，且都是单页文件
    assert len(uploads) == 3
    from pypdf import PdfReader

    for upload in uploads:
        assert upload["pdf_file_num"] == "1"
        reader = PdfReader(io.BytesIO(base64.b64decode(upload["pdf_file"])))
        assert len(reader.pages) == 1


def test_split_falls_back_to_page_images(monkeypatch, tmp_path):
    """单页仍然超限时，把该页内嵌的扫描图片拆出来压缩后单独上传。"""
    path = tmp_path / "scan.pdf"
    path.write_bytes(_pdf_with_images(_jpeg_bytes((900, 700))))
    uploads = []

    def handler(request):
        if "oauth" in request.full_url:
            return _FakeResponse(json.dumps({"access_token": "tk", "expires_in": 60}).encode())
        body = urllib.parse.parse_qs(request.data.decode())
        uploads.append({key: value[0] for key, value in body.items()})
        return _FakeResponse(json.dumps({"words_result": [{"words": "扫描文字"}]}).encode())

    _patch_urlopen(monkeypatch, handler)
    # 单页 PDF 大于上限，但压缩后的图片可以放进上限内
    monkeypatch.setattr(
        ocr.BaiduOCRProvider, "budget_bytes", property(lambda self: 20 * 1024)
    )
    provider = ocr.BaiduOCRProvider("ak", "sk")
    results = provider.extract_pages(path, [1])
    assert results == {1: "扫描文字"}
    assert uploads and "image" in uploads[0]
    assert "pdf_file" not in uploads[0]
    # 上传的图片已被压缩到上限以内
    assert len(base64.b64decode(uploads[0]["image"])) <= 20 * 1024


# --- 扫描件（页内嵌图片）---------------------------------------------------


def _jpeg_bytes(size=(320, 240)) -> bytes:
    from PIL import Image

    image = Image.new("RGB", size, (210, 200, 190))
    for x in range(0, size[0], 5):
        for y in range(0, size[1], 5):
            image.putpixel((x, y), (30, 40, 50))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _pdf_with_images(jpeg: bytes, pages: int = 2) -> bytes:
    """构造一个每页内嵌一张 JPEG 的扫描件 PDF。"""
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>",
    }
    content = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    for page_no, (page_obj, content_obj, image_obj) in enumerate(
        [(3, 4, 5), (6, 7, 8)], start=1
    ):
        objects[content_obj] = (
            b"<</Length %d>>stream\n" % len(content) + content + b"\nendstream"
        )
        objects[image_obj] = (
            b"<</Type/XObject/Subtype/Image/Width 320/Height 240/ColorSpace/DeviceRGB"
            b"/BitsPerComponent 8/Filter/DCTDecode/Length %d>>stream\n" % len(jpeg)
            + jpeg
            + b"\nendstream"
        )
        objects[page_obj] = (
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Resources<</XObject<</Im0 %d 0 R>>>>/Contents %d 0 R>>"
            % (image_obj, content_obj)
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for number in sorted(objects):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_extract_page_images_reads_embedded_scan(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(_pdf_with_images(_jpeg_bytes()))
    images = ocr.extract_page_images(path, 1)
    assert len(images) == 1
    assert images[0][:2] == b"\xff\xd8"  # JPEG 文件头
    assert len(ocr.extract_page_images(path, 2)) == 1


def test_fit_image_shrinks_to_budget():
    from PIL import Image

    source = _jpeg_bytes((900, 700))
    fitted = ocr.fit_image(source, 20 * 1024)
    assert fitted is not None
    assert len(fitted) <= 20 * 1024
    # 压缩后仍然是可识别的图片（必要时等比缩小）
    image = Image.open(io.BytesIO(fitted))
    assert image.size[0] <= 900 and image.size[1] <= 700


def test_fit_image_keeps_small_image_untouched():
    source = _jpeg_bytes((40, 30))
    assert ocr.fit_image(source, 1024 * 1024) is source


def test_scanned_pdf_is_split_and_uploaded_per_page(monkeypatch, tmp_path):
    """扫描件超过单次上限时按页拆分，逐页上传。"""
    path = tmp_path / "scan.pdf"
    path.write_bytes(_pdf_with_images(_jpeg_bytes()))
    uploads = []

    def handler(request):
        if "oauth" in request.full_url:
            return _FakeResponse(json.dumps({"access_token": "tk", "expires_in": 60}).encode())
        body = urllib.parse.parse_qs(request.data.decode())
        uploads.append({key: value[0] for key, value in body.items()})
        return _FakeResponse(json.dumps({"words_result": [{"words": "扫描页文字"}]}).encode())

    _patch_urlopen(monkeypatch, handler)
    monkeypatch.setattr(
        ocr.BaiduOCRProvider, "budget_bytes", property(lambda self: 4000)
    )
    provider = ocr.BaiduOCRProvider("ak", "sk")
    results = provider.extract_pages(path, [1, 2])
    assert results == {1: "扫描页文字", 2: "扫描页文字"}
    assert provider.last_split is True
    assert len(uploads) == 2
    from pypdf import PdfReader

    for upload in uploads:
        # 单页仍然超过 4000 字节时退回「拆出页面图片」上传
        assert "image" in upload or "pdf_file" in upload
        if "pdf_file" in upload:
            reader = PdfReader(io.BytesIO(base64.b64decode(upload["pdf_file"])))
            assert len(reader.pages) == 1


# --- 规则维度 ---------------------------------------------------------------


def test_ocr_dimension_is_initialized(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    record = rule_service.active_version(db, "ocr")
    assert record is not None
    assert record.params["ocr_enabled"] is False
    assert record.params["ocr_provider"] == "baidu"


def test_ocr_is_excluded_from_analysis_snapshot(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    snapshot = rule_service.active_snapshot(db)
    assert "ocr" not in snapshot
    assert "text" in snapshot


def test_secrets_are_masked_in_payload(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    rule_service.create_version(
        db,
        "ocr",
        {"baidu_api_key": "ak-real", "baidu_secret_key": "sk-real", "ocr_enabled": True},
        "配置百度 OCR",
        activate=True,
    )
    db.commit()
    record = rule_service.active_version(db, "ocr")
    payload = rule_service.version_payload(record)
    assert payload["params"]["baidu_api_key"] == rule_service.SECRET_MASK
    assert payload["params"]["baidu_secret_key"] == rule_service.SECRET_MASK
    assert payload["has_secrets"] is True
    # 真实值仍然保存在库里
    assert record.params["baidu_api_key"] == "ak-real"


def test_secret_placeholder_keeps_existing_value(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    rule_service.create_version(
        db,
        "ocr",
        {"baidu_api_key": "ak-real", "baidu_secret_key": "sk-real"},
        "配置百度 OCR",
        activate=True,
    )
    db.commit()
    # 前端回传脱敏占位符时，服务端保留真实值
    rule_service.create_version(
        db,
        "ocr",
        {"baidu_api_key": rule_service.SECRET_MASK, "baidu_secret_key": rule_service.SECRET_MASK},
        "只改开关",
        activate=True,
    )
    db.commit()
    record = rule_service.active_version(db, "ocr")
    assert record.params["baidu_api_key"] == "ak-real"
    assert record.params["baidu_secret_key"] == "sk-real"


def test_enabling_ocr_requires_credentials(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    with pytest.raises(rule_service.RuleError):
        rule_service.create_version(db, "ocr", {"ocr_enabled": True}, "直接开启")
    with pytest.raises(rule_service.RuleError):
        rule_service.create_version(
            db, "ocr", {"ocr_enabled": True, "baidu_api_key": "ak"}, "只填一个"
        )
    # 超过接口上限的体积会被收敛到 6MB（原始文件上限）
    record = rule_service.create_version(db, "ocr", {"ocr_max_file_mb": 20}, "设置上限")
    assert record.params["ocr_max_file_mb"] == ocr.MAX_RAW_FILE_MB


def test_active_ocr_config_reads_active_version(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    config = rule_service.active_ocr_config(db)
    assert config["ocr_enabled"] is False
    rule_service.create_version(
        db,
        "ocr",
        {
            "ocr_enabled": True,
            "baidu_api_key": "ak",
            "baidu_secret_key": "sk",
            "baidu_language_type": "ENG",
        },
        "启用 OCR",
        activate=True,
    )
    db.commit()
    config = rule_service.active_ocr_config(db)
    assert config["ocr_enabled"] is True
    assert config["baidu_language_type"] == "ENG"


def test_ocr_version_history_is_preserved(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    rule_service.create_version(
        db,
        "ocr",
        {"ocr_enabled": True, "baidu_api_key": "ak", "baidu_secret_key": "sk"},
        "启用 OCR",
        activate=True,
    )
    db.commit()
    versions = list(
        db.query(DetectionRuleVersion).filter(DetectionRuleVersion.dimension == "ocr").all()
    )
    assert len(versions) == 2
    assert any(version.status == RuleStatus.ARCHIVED.value for version in versions)


# --- 接口 -------------------------------------------------------------------


def _login(client, username="admin", password="admin123"):
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_rules_api_masks_secrets_for_analyst(db):
    from fastapi.testclient import TestClient

    from app.core.security import hash_password
    from app.main import app
    from app.models import User

    rule_service.ensure_initial_version(db)
    db.commit()
    rule_service.create_version(
        db,
        "ocr",
        {"ocr_enabled": True, "baidu_api_key": "ak-real", "baidu_secret_key": "sk-real"},
        "配置百度 OCR",
        activate=True,
    )
    db.commit()
    db.add(
        User(
            username="analyst",
            password_hash=hash_password("secret"),
            display_name="分析员",
            role="analyst",
        )
    )
    db.commit()

    with TestClient(app) as client:
        headers = _login(client, "analyst", "secret")
        payload = client.get("/api/v1/system/rules", headers=headers).json()
        ocr_dimension = next(
            item for item in payload["dimensions"] if item["dimension"] == "ocr"
        )
        assert ocr_dimension["active"]["params"]["baidu_api_key"] == rule_service.SECRET_MASK
        assert "ak-real" not in json.dumps(payload, ensure_ascii=False)
        assert payload["can_edit"] is False
        # 非管理员不能新建规则版本
        created = client.post(
            "/api/v1/system/rules/versions",
            json={"dimension": "ocr", "params": {}, "change_reason": "越权修改"},
            headers=headers,
        )
        assert created.status_code == 403


def test_ocr_test_endpoint_requires_admin(db):
    from fastapi.testclient import TestClient

    from app.core.security import hash_password
    from app.main import app
    from app.models import User

    db.add(
        User(
            username="reviewer",
            password_hash=hash_password("secret"),
            display_name="审核员",
            role="reviewer",
        )
    )
    db.commit()
    with TestClient(app) as client:
        headers = _login(client, "reviewer", "secret")
        response = client.post("/api/v1/system/ocr/test", json={}, headers=headers)
        assert response.status_code == 403
