"""图片感知哈希（pHash / dHash / aHash）.

标书里大量内容以图片形式存在：印章、资质证书、业绩截图、组织结构图、
扫描插入的技术图纸。两家企业的标书里出现**同一张图片**，比文字雷同更难用
「大家都这么写」解释——尤其是盖在落款处的同一枚公章、同一张证书扫描件。

哈希选型：只依赖 Pillow，不引入 imagehash 之类额外库。

- ``dHash``（差值哈希）：把图像缩放成 9×8 灰度，比较横向相邻像素大小关系，
  得到 64 位。对亮度整体变化、轻微压缩不敏感，是本场景的主力。
- ``aHash``（均值哈希）：缩放成 8×8 后与均值比较。作为第二票补充。
- 完全相同的字节（``sha256``）单独记录：它是「同一张图」的铁证。

判定：**字节哈希相同** → 高置信；**汉明距离 ≤ 阈值** → 近似同源。阈值默认 6/64
（约 9% 像素差异），足以容忍重新导出与 JPEG 重压缩，又不会把普通照片判成同一张。

Pillow 未安装时整块功能自动关闭（返回空），不阻断文件解析——生产镜像里
Pillow 是固定依赖，本地精简环境才可能缺失。
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 竖版证书/扫描件常见长宽比，这里统一缩放尺寸便于比较
_DHASH_SIZE = (9, 8)
_AHASH_SIZE = (8, 8)
# 过小的图（图标、项目符号）不参与比对，避免误报
_MIN_PIXEL_AREA = 32 * 32
# 单张图的字节上限：超大位图内存占用高且多为扫描页，交给 OCR 处理
_MAX_BYTES = 8 * 1024 * 1024

try:  # pragma: no cover - 依赖缺失只影响本模块能力
    from PIL import Image

    HAS_PILLOW = True
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    HAS_PILLOW = False


@dataclass
class ImageFingerprint:
    """一张图片的指纹。"""

    sha256: str
    dhash: int
    ahash: int
    width: int
    height: int

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "dhash": format(self.dhash, "016x"),
            "ahash": format(self.ahash, "016x"),
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageFingerprint | None":
        try:
            return cls(
                sha256=str(data["sha256"]),
                dhash=int(data["dhash"], 16),
                ahash=int(data["ahash"], 16),
                width=int(data.get("width") or 0),
                height=int(data.get("height") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _bits_from_pixels(pixels, size: int) -> int:
    value = 0
    for bit in pixels:
        value = (value << 1) | (1 if bit else 0)
    return value


def _thumbnail(image, size: tuple[int, int]):
    """先缩到接近目标尺寸再细缩。

    扫描页内嵌图常有 3000×4000 像素，直接从原图用 LANCZOS 缩到 9×8 非常慢
    （实测单页上百毫秒）。先用 NEAREST 粗缩到 4 倍目标尺寸，再精细缩放，
    视觉噪点极少但耗时下降一个量级。
    """
    coarse = image.resize(
        (max(size[0] * 4, 1), max(size[1] * 4, 1)), Image.NEAREST
    )
    return coarse.resize(size, Image.LANCZOS)


def _is_low_information(image) -> bool:
    """判断图片是否几乎没有结构（纯色、空白扫描页、单色底纹）。

    这类图的 dHash 恒为 0、aHash 恒为全 1，任意两张都会被判成「完全相同」。
    标书里空白页、纯色封面很常见，必须排除掉，否则会制造大量假阳性。
    用灰度标准差做判据：低于 3 基本就是单一颜色。
    """
    small = _thumbnail(image.convert("L"), (32, 32))
    pixels = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    variance = sum((value - mean) ** 2 for value in pixels) / len(pixels)
    return variance**0.5 < 3.0


def _dhash(image) -> int:
    small = _thumbnail(image.convert("L"), _DHASH_SIZE)
    pixels = list(small.getdata())
    width, height = small.size
    bits = []
    for row in range(height):
        offset = row * width
        for col in range(width - 1):
            bits.append(pixels[offset + col] > pixels[offset + col + 1])
    return _bits_from_pixels(bits, width * height)


def _ahash(image) -> int:
    small = _thumbnail(image.convert("L"), _AHASH_SIZE)
    pixels = list(small.getdata())
    average = sum(pixels) / len(pixels)
    return _bits_from_pixels((value >= average for value in pixels), len(pixels))


def fingerprint_bytes(data: bytes) -> ImageFingerprint | None:
    """对一张图片的原始字节计算指纹；不可识别或过小时返回 None。"""
    if not HAS_PILLOW or not data or len(data) > _MAX_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height < _MIN_PIXEL_AREA:
                return None
            # 先转成 RGB，避免调色板/CMYK 在 resize 时出问题
            converted = image.convert("RGB")
            # 纯色/空白图不参与比对：它们的感知哈希彼此相同，没有鉴别力
            if _is_low_information(converted):
                return None
            return ImageFingerprint(
                sha256=hashlib.sha256(data).hexdigest(),
                dhash=_dhash(converted),
                ahash=_ahash(converted),
                width=converted.width,
                height=converted.height,
            )
    except Exception as exc:  # 损坏或非图片数据
        logger.debug("图片指纹计算失败: %s", exc)
        return None


def extract_pdf_images(path, *, max_images: int = 200) -> list[bytes]:
    """从 PDF 页面里取出内嵌图片的原始字节。"""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return []
    images: list[bytes] = []
    try:
        reader = PdfReader(str(path))
        for page in reader.pages:
            for image in getattr(page, "images", []):
                data = getattr(image, "data", None)
                if data:
                    images.append(data)
                    if len(images) >= max_images:
                        return images
    except Exception as exc:
        logger.info("PDF 图片提取失败 %s: %s", path, exc)
    return images


def extract_docx_images(path, *, max_images: int = 200) -> list[bytes]:
    """从 DOCX 的 word/media 目录取图片。"""
    import zipfile

    images: list[bytes] = []
    try:
        with zipfile.ZipFile(str(path)) as archive:
            for name in archive.namelist():
                if not name.startswith("word/media/"):
                    continue
                try:
                    images.append(archive.read(name))
                except Exception:
                    continue
                if len(images) >= max_images:
                    break
    except Exception as exc:
        logger.info("DOCX 图片提取失败 %s: %s", path, exc)
    return images


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def build_image_fingerprints(path, file_type: str) -> list[dict]:
    """提取并计算一个文件内全部图片的指纹。"""
    if not HAS_PILLOW:
        return []
    file_type = (file_type or "").lower().lstrip(".")
    if file_type == "pdf":
        raw_images = extract_pdf_images(path)
    elif file_type in {"docx", "wps"}:
        raw_images = extract_docx_images(path)
    else:
        return []

    seen: set[str] = set()
    result: list[dict] = []
    for raw in raw_images:
        fingerprint = fingerprint_bytes(raw)
        if fingerprint is None or fingerprint.sha256 in seen:
            continue
        seen.add(fingerprint.sha256)
        result.append(fingerprint.to_dict())
    return result


def compare_image_sets(
    images_a: list[dict],
    images_b: list[dict],
    *,
    dhash_threshold: int = 6,
    ahash_threshold: int = 10,
) -> list[dict]:
    """比对两组图片指纹，返回同源图片对。

    判定用**两把哈希同时通过**而不是只看 dHash：纯渐变、等距条纹这类图片的
    dHash 会退化成全 0（相邻像素恒定单调），只比 dHash 会互相撞车；aHash 能
    区分它们。两把都通过才认，宁可漏一点也不错配。
    """
    fingerprints_a = [
        (item, ImageFingerprint.from_dict(item)) for item in images_a
    ]
    fingerprints_b = [
        (item, ImageFingerprint.from_dict(item)) for item in images_b
    ]
    fingerprints_a = [(raw, fp) for raw, fp in fingerprints_a if fp]
    fingerprints_b = [(raw, fp) for raw, fp in fingerprints_b if fp]

    matches: list[dict] = []
    used_b: set[str] = set()
    for raw_a, fp_a in fingerprints_a:
        for raw_b, fp_b in fingerprints_b:
            if fp_b.sha256 in used_b:
                continue
            identical = fp_a.sha256 == fp_b.sha256
            d_distance = hamming_distance(fp_a.dhash, fp_b.dhash)
            a_distance = hamming_distance(fp_a.ahash, fp_b.ahash)
            if identical or (d_distance <= dhash_threshold and a_distance <= ahash_threshold):
                used_b.add(fp_b.sha256)
                matches.append(
                    {
                        "identical": identical,
                        "hamming_distance": d_distance,
                        "ahash_distance": a_distance,
                        "image_a": raw_a,
                        "image_b": raw_b,
                    }
                )
                break
    return matches
