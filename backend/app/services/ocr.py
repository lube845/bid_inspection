"""OCR 提供方。

V1 接入百度智能云 OCR「通用文字识别（标准含位置版）」：

    识别接口：POST https://aip.baidubce.com/rest/2.0/ocr/v1/general
    鉴权接口：GET  https://aip.baidubce.com/oauth/2.0/token
               ?grant_type=client_credentials
               &client_id=<API Key>&client_secret=<Secret Key>
    接口文档：https://cloud.baidu.com/doc/OCR/s/vk3h7y58v

调用约定：

- 扫描版 PDF：使用 ``pdf_file``（base64）+ ``pdf_file_num`` 指定页码；
- 图片文件：使用 ``image``（base64）；
- 请求体为 ``application/x-www-form-urlencoded``，base64 之后必须再 urlencode；
- 单次请求的 base64 + urlencode 体积不能超过 8MB。

体积控制：base64 会把原始文件放大约 4/3，因此「单次 OCR 文件大小上限」按原始
文件计算（默认 6MB，最大 6MB），对应百度接口编码后的 8MB 硬限制。超过上限的
PDF 会先被拆成单页文件，再逐份上传识别，避免整份大文件被接口拒绝。

识别失败时返回 None（页面被标记为未识别），不伪造文本，也不中断整个文件的解析。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

BAIDU_OCR_ENDPOINT = "https://aip.baidubce.com/rest/2.0/ocr/v1/general"
BAIDU_TOKEN_ENDPOINT = "https://aip.baidubce.com/oauth/2.0/token"

# 百度通用文字识别支持的语言
BAIDU_LANGUAGE_TYPES = [
    "CHN_ENG",
    "ENG",
    "JAP",
    "KOR",
    "FRE",
    "SPA",
    "POR",
    "GER",
    "ITA",
    "RUS",
]

# 原始文件体积上限：base64 之后约放大 4/3，再留一点 urlencode 的余量
MAX_RAW_FILE_MB = 6


class OCRError(Exception):
    """OCR 调用或配置错误。"""


class OCRProvider(ABC):
    """OCR 提供方接口。

    未启用 OCR 或识别失败时返回 None，扫描页会被标记为「空白/需 OCR」，
    文件状态为部分成功，不会伪造识别文本。
    """

    #: 供界面展示的提供方名称
    name: str = "OCR"

    @abstractmethod
    def extract_text(self, file_path: Path, page_index: int) -> str | None:
        raise NotImplementedError

    def extract_pages(
        self, file_path: Path, page_indexes: list[int]
    ) -> dict[int, str | None]:
        """批量识别多个页面。

        默认逐页调用 :meth:`extract_text`；支持批量上传的提供方可以重写，
        一次读取文件、拆分后再逐份上传，减少重复的磁盘与网络开销。
        """
        return {index: self.extract_text(file_path, index) for index in page_indexes}

    def describe(self) -> str:
        """当前识别器的状态说明，用于写入页面备注。"""
        return self.name


class NoopOCRProvider(OCRProvider):
    name = "OCR 未启用"

    def extract_text(self, file_path: Path, page_index: int) -> str | None:
        return None


# access_token 缓存：键为 (api_key, secret_key)，值为 (token, 过期时间戳)
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


def clear_token_cache() -> None:
    """清空 access_token 缓存（更换凭据或测试连接时调用）。"""
    _TOKEN_CACHE.clear()


def fetch_access_token(
    api_key: str, secret_key: str, timeout: int = 30, force: bool = False
) -> str:
    """获取百度 OCR 的 access_token，并按有效期缓存。"""
    api_key = (api_key or "").strip()
    secret_key = (secret_key or "").strip()
    if not api_key or not secret_key:
        raise OCRError("百度 OCR 需要同时配置 API Key 与 Secret Key")
    cache_key = (api_key, secret_key)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and not force and cached[1] > time.time() + 60:
        return cached[0]
    query = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        }
    )
    request = urllib.request.Request(
        f"{BAIDU_TOKEN_ENDPOINT}?{query}", method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:300]
        raise OCRError(f"获取 access_token 失败（HTTP {exc.code}）：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OCRError(f"获取 access_token 网络请求失败：{exc}") from exc
    token = payload.get("access_token")
    if not token:
        message = payload.get("error_description") or payload.get("error") or "未知错误"
        raise OCRError(f"获取 access_token 失败：{message}")
    expires_in = int(payload.get("expires_in") or 3600)
    _TOKEN_CACHE[cache_key] = (token, time.time() + expires_in)
    return token


@dataclass
class PdfPart:
    """拆分出来的一个待识别文件。

    ``pages`` 是「原始页码 → 该文件内的页码」的映射；拆分后的文件通常只含
    一页，因此映射值固定为 1。
    """

    data: bytes
    pages: dict[int, int] = field(default_factory=dict)
    #: 该文件是从哪一页拆出来的（用于日志）
    origin: str = ""


def split_pdf_pages(path: Path, page_indexes: list[int], budget: int) -> list[PdfPart]:
    """把 PDF 中指定的页拆成单页文件，每个文件不超过 ``budget`` 字节。

    整份 PDF 直接上传时只需一次请求，但体积超过上限就会被接口拒绝；拆成单页
    文件后每页单独上传，既满足体积限制，也避免同一份数据被重复发送多次。
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise OCRError("缺少 PDF 解析依赖 pypdf") from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise OCRError(f"PDF 无法打开: {exc}") from exc

    parts: list[PdfPart] = []
    total = len(reader.pages)
    for index in page_indexes:
        if index < 1 or index > total:
            continue
        try:
            writer = PdfWriter()
            writer.add_page(reader.pages[index - 1])
            buffer = io.BytesIO()
            writer.write(buffer)
            data = buffer.getvalue()
        except Exception as exc:
            logger.warning("拆分 PDF 第 %s 页失败：%s", index, exc)
            continue
        parts.append(PdfPart(data=data, pages={index: 1}, origin=f"第 {index} 页"))
    return parts


def fit_image(data: bytes, budget: int) -> bytes | None:
    """把图片压缩到不超过 ``budget`` 字节。

    扫描件的单页图片可能远超接口上限，这里按「先降质量、再等比缩小」的顺序
    压到限制以内；压不进去（例如尺寸极小但色彩极多）时返回 None，由调用方跳过。
    """
    if len(data) <= budget:
        return data
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - pillow 是运行依赖
        logger.warning("缺少 Pillow，超大图片无法压缩")
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        logger.info("图片解码失败：%s", exc)
        return None
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    quality = 85
    for _ in range(6):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
        encoded = buffer.getvalue()
        if len(encoded) <= budget:
            return encoded
        quality = max(35, int(quality * 0.75))
    width, height = image.size
    for _ in range(6):
        width = max(64, int(width * 0.75))
        height = max(64, int(height * 0.75))
        buffer = io.BytesIO()
        image.resize((width, height)).save(buffer, format="JPEG", quality=60)
        encoded = buffer.getvalue()
        if len(encoded) <= budget:
            return encoded
    return None


def extract_page_images(path: Path, page_index: int) -> list[bytes]:
    """取出 PDF 某一页里内嵌的图片原始字节。

    扫描件的正文通常就是一整张图片；当单页 PDF 仍然超过体积上限时，可以把这些
    图片拆出来单独上传（必要时先压缩，见 :func:`fit_image`）。
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise OCRError("缺少 PDF 解析依赖 pypdf") from exc
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise OCRError(f"PDF 无法打开: {exc}") from exc
    if page_index < 1 or page_index > len(reader.pages):
        return []
    page = reader.pages[page_index - 1]
    images: list[bytes] = []
    try:
        for image in page.images:
            data = image.data
            if data:
                images.append(bytes(data))
    except Exception as exc:  # 部分图片格式需要 Pillow 才能解码
        logger.info("读取第 %s 页内嵌图片失败：%s", page_index, exc)
    return images


class BaiduOCRProvider(OCRProvider):
    """百度智能云 OCR 提供方。"""

    name = "百度 OCR"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        language_type: str = "CHN_ENG",
        detect_direction: bool = True,
        probability: bool = False,
        timeout: int = 60,
        max_file_mb: int = MAX_RAW_FILE_MB,
        endpoint: str = BAIDU_OCR_ENDPOINT,
    ):
        self.api_key = (api_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        if not self.api_key or not self.secret_key:
            raise OCRError("百度 OCR 需要同时配置 API Key 与 Secret Key")
        self.language_type = (
            language_type if language_type in BAIDU_LANGUAGE_TYPES else "CHN_ENG"
        )
        self.detect_direction = bool(detect_direction)
        self.probability = bool(probability)
        self.timeout = max(5, int(timeout or 60))
        # 单次上传的原始文件体积上限：base64 之后不能超过百度接口的 8MB
        self.max_file_mb = max(
            1, min(int(max_file_mb or MAX_RAW_FILE_MB), MAX_RAW_FILE_MB)
        )
        self.endpoint = endpoint
        #: 最近一次识别是否按页拆分上传（PDF 恒为 True）
        self.last_split = False

    @property
    def budget_bytes(self) -> int:
        """单次上传允许的原始文件字节数。"""
        return self.max_file_mb * 1024 * 1024

    def extract_text(self, file_path: Path, page_index: int) -> str | None:
        return self.extract_pages(Path(file_path), [page_index]).get(page_index)

    def extract_pages(
        self, file_path: Path, page_indexes: list[int]
    ) -> dict[int, str | None]:
        """批量识别：PDF 按页拆成单页文件逐份上传，图片整张上传。

        百度的接口一次只识别一页（``pdf_file_num``），如果每次都上传整份 PDF，
        N 页就要传输 N 份完整文件；拆成单页文件后总传输量约等于原文件大小，
        而且每份文件都天然满足单次体积上限。
        """
        path = Path(file_path)
        results: dict[int, str | None] = {}
        if not page_indexes:
            return results
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise OCRError(f"读取文件失败：{exc}") from exc

        if path.suffix.lower() != ".pdf":
            for index in page_indexes:
                results[index] = self._recognize_image(raw)
            return results

        self.last_split = True
        try:
            parts = split_pdf_pages(path, page_indexes, self.budget_bytes)
        except OCRError as exc:
            # 文件本身无法拆分（例如格式损坏）：该文件所有页标记为未识别
            logger.warning("拆分 PDF 失败：%s", exc)
            parts = []
        for part in parts:
            for origin_index, part_index in part.pages.items():
                if len(part.data) <= self.budget_bytes:
                    results[origin_index] = self._recognize_pdf(part.data, part_index)
                    continue
                # 单页仍然过大：把这页内嵌的扫描图片拆出来单独上传
                results[origin_index] = self._recognize_page_images(path, origin_index)
        for index in page_indexes:
            results.setdefault(index, None)
        return results

    def _recognize_page_images(self, path: Path, page_index: int) -> str | None:
        """把整页里的扫描图片逐张识别后拼接。"""
        images = extract_page_images(path, page_index)
        if not images:
            # 没有可拆的图片时仍然尝试整页上传，让接口给出明确错误
            try:
                parts = split_pdf_pages(path, [page_index], self.budget_bytes)
                if parts:
                    return self._recognize_pdf(parts[0].data, 1)
            except OCRError:
                pass
            return None
        texts: list[str] = []
        for data in images:
            fitted = fit_image(data, self.budget_bytes)
            if fitted is None:
                logger.warning(
                    "第 %s 页的图片压缩后仍超过 %sMB，已跳过",
                    page_index,
                    self.max_file_mb,
                )
                continue
            text = self._recognize_image(fitted)
            if text:
                texts.append(text)
        return "\n".join(texts).strip() or None

    def _recognize_pdf(self, raw: bytes, page_index: int) -> str | None:
        try:
            payload = self._request(
                {
                    "pdf_file": base64.b64encode(raw).decode("ascii"),
                    "pdf_file_num": str(max(1, int(page_index))),
                }
            )
        except OCRError as exc:
            logger.warning("百度 OCR 识别 PDF 第 %s 页失败：%s", page_index, exc)
            return None
        return self._words_to_text(payload)

    def _recognize_image(self, raw: bytes) -> str | None:
        try:
            payload = self._request({"image": base64.b64encode(raw).decode("ascii")})
        except OCRError as exc:
            logger.warning("百度 OCR 识别图片失败：%s", exc)
            return None
        return self._words_to_text(payload)

    @staticmethod
    def _words_to_text(payload: dict) -> str | None:
        words = [
            str(item.get("words") or "")
            for item in (payload.get("words_result") or [])
            if item.get("words")
        ]
        text = "\n".join(words).strip()
        return text or None

    def _request(self, extra: dict) -> dict:
        form = {
            "language_type": self.language_type,
            "detect_direction": "true" if self.detect_direction else "false",
            "probability": "true" if self.probability else "false",
        }
        form.update(extra)
        body = urllib.parse.urlencode(form).encode("utf-8")
        token = fetch_access_token(self.api_key, self.secret_key, timeout=self.timeout)
        url = f"{self.endpoint}?access_token={urllib.parse.quote(token)}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            raise OCRError(f"百度 OCR 返回 HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OCRError(f"百度 OCR 网络请求失败：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise OCRError(f"百度 OCR 返回内容不是合法 JSON：{exc}") from exc
        if payload.get("error_code"):
            raise OCRError(
                f"百度 OCR 错误 {payload.get('error_code')}：{payload.get('error_msg')}"
            )
        return payload


def build_ocr_provider(config: dict | None) -> OCRProvider:
    """按当前 OCR 配置构造识别器。

    未启用或配置不完整时返回空实现，解析流程照常进行（扫描页标记为未识别）。
    """
    config = config or {}
    if not config.get("ocr_enabled"):
        return NoopOCRProvider()
    provider = str(config.get("ocr_provider") or "").strip().lower()
    if provider == "baidu":
        try:
            return BaiduOCRProvider(
                api_key=config.get("baidu_api_key"),
                secret_key=config.get("baidu_secret_key"),
                language_type=config.get("baidu_language_type") or "CHN_ENG",
                detect_direction=bool(config.get("baidu_detect_direction", True)),
                probability=bool(config.get("baidu_probability", False)),
                timeout=int(config.get("ocr_timeout_seconds") or 60),
                max_file_mb=int(config.get("ocr_max_file_mb") or MAX_RAW_FILE_MB),
            )
        except OCRError as exc:
            logger.warning("百度 OCR 未启用：%s", exc)
            return NoopOCRProvider()
    return NoopOCRProvider()
