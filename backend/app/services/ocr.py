from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path


class OCRProvider(ABC):
    """OCR 提供方接口。

    V1 只保留框架：未接入模型时返回 None，扫描页会被标记为“空白/需 OCR”，
    文件状态为部分成功，不会伪造识别文本。
    """

    @abstractmethod
    def extract_text(self, file_path: Path, page_index: int) -> str | None:
        raise NotImplementedError


class NoopOCRProvider(OCRProvider):
    def extract_text(self, file_path: Path, page_index: int) -> str | None:
        return None


class TesseractOCRProvider(OCRProvider):  # pragma: no cover - 示例实现，默认不启用
    """接入示例：安装 pytesseract + pdf2image 后启用。"""

    def extract_text(self, file_path: Path, page_index: int) -> str | None:
        return None


@lru_cache
def get_ocr_provider() -> OCRProvider:
    return NoopOCRProvider()
