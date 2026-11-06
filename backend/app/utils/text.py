import re
import unicodedata
import zlib

# 规范化时忽略的标点与空白：算法比较用规范化文本，展示始终使用原文。
_IGNORED = set(
    " \t\r\n　,，.。;；:：!！?？、\"'“”‘’()（）[]【】{}<>《》〈〉「」『』"
    "〔〕〖〗〘〙〚〛—_-–~～…·．｡､"
)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回规范化文本以及每个字符在原文中的下标。

    这个下标映射用于把算法命中的偏移量还原到原始文本，避免将规范化文本的
    偏移量直接用于原文定位。
    """
    chars: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text):
        if char in _IGNORED or char.isspace():
            continue
        # NFKC 统一全角/半角与兼容字符，并保留每个规范化字符的原文下标
        for part in unicodedata.normalize("NFKC", char):
            if part in _IGNORED or part.isspace() or unicodedata.combining(part):
                continue
            code = ord(part)
            if 0xFF01 <= code <= 0xFF5E:
                part = chr(code - 0xFEE0)
            elif code == 0x3000:
                continue
            chars.append(part.lower())
            index_map.append(index)
    return "".join(chars), index_map


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


def shingles(text: str, size: int) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def stable_hash(text: str) -> int:
    """稳定的 32 位指纹。

    Python 内置 hash() 每个进程都会加盐，不能用于持久化结果和跨任务缓存；
    crc32 是确定性的，且由 C 实现，生成字符级 Shingle 时开销足够低。
    """
    return zlib.crc32(text.encode("utf-8"))


def shingle_hashes(text: str, size: int) -> set[int]:
    """字符级 Shingle 指纹集合（稳定、可复现）。"""
    if not text:
        return set()
    if len(text) < size:
        return {stable_hash(text)}
    return {stable_hash(text[i : i + size]) for i in range(len(text) - size + 1)}


def compact_snippet(text: str, limit: int = 160) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "…"
