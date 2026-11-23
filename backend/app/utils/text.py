import re
import unicodedata
import zlib

# 规范化时忽略的标点与空白：算法比较用规范化文本，展示始终使用原文。
#
# 分三类：
# - 中英文常用标点（含全角/半角两套写法）；
# - 纯排版符号（货币、百分号、公式、表格竖线等）。NFKC 会把全角形式降成半角，
#   所以这里只需登记半角字符即可同时覆盖全角输入（如 ％→%、￥→¥）。
#   这些符号在不同投标文件里的写法差异（"金额:￥100万" vs "金额:100万"、
#   "2024/01/01" vs "2024-01-01"）只属于排版，不应影响相似度判定。
# - 零宽与格式控制字符（见 _is_invisible）。它们渲染时不可见，却会把一个词切
#   成两个 Shingle，是绕过查重最省力的手法：往「投标报价」里插 1～3 个 U+200B
#   就能让字面相似度掉到接近 0，而评标人看到的页面完全一样。
_IGNORED = set(
    " \t\r\n　,，.。;；:：!！?？、\"'“”‘’()（）[]【】{}<>《》〈〉「」『』"
    "〔〕〖〗〘〙〚〛—_-–~～…·．｡､"
    "#$%&*+/=@\\^`|¥"
)

# 零宽字符与格式控制字符：NFKC 不会处理它们，必须显式丢弃。
# 这里只列「确定不可见且不承载语义」的字符，避免误删真正的内容。
_INVISIBLE_CATEGORIES = {"Cf", "Zl", "Zp", "Mn", "Me"}
_SOFT_HYPHEN = "­"

# 同形字：西里尔/希腊字母里与拉丁字母同形的字符。
# 标书的技术指标、型号常用英文缩写，攻方会把其中一个字母换成外观相同的
# 西里尔字母，肉眼与打印效果都看不出来，但字面比对会整段失效。
_HOMOGLYPHS = str.maketrans(
    {
        # 西里尔小写
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y",
        "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
        "ց": "g", "հ": "h", "ո": "n", "ս": "u", "ԛ": "q", "ԝ": "w",
        # 西里尔大写
        "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
        "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
        "І": "I", "Ѕ": "S", "Ј": "J",
        # 希腊小写
        "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
        "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "γ": "y",
        # 希腊大写
        "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
        "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
        "Υ": "Y", "Χ": "X",
    }
)


def _is_invisible(char: str) -> bool:
    """渲染时不可见、且不承载语义的字符。

    判定用 Unicode 通用类别而不是枚举码位，这样新增的格式字符（例如各版本
    变体选择符）也会被覆盖；Mn/Me 是组合用标记，中文正文里不会出现，
    删除它们只会让「带附加符号的字母」回归到基础字母。
    """
    if char in _IGNORED or char.isspace():
        return True
    if char == _SOFT_HYPHEN:
        return True
    return unicodedata.category(char) in _INVISIBLE_CATEGORIES


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回规范化文本以及每个字符在原文中的下标。

    这个下标映射用于把算法命中的偏移量还原到原始文本，避免将规范化文本的
    偏移量直接用于原文定位。
    """
    chars: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text):
        if _is_invisible(char):
            continue
        # NFKC 统一全角/半角与兼容字符，并保留每个规范化字符的原文下标
        for part in unicodedata.normalize("NFKC", char):
            if _is_invisible(part) or unicodedata.combining(part):
                continue
            code = ord(part)
            if 0xFF01 <= code <= 0xFF5E:
                part = chr(code - 0xFEE0)
            elif code == 0x3000:
                continue
            # 同形字归一放在 NFKC 之后：西里尔/希腊字母是独立码位，
            # NFKC 不会改写，必须显式映射成对应的拉丁字母。
            part = part.translate(_HOMOGLYPHS)
            chars.append(part.lower())
            index_map.append(index)
    return "".join(chars), index_map


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


def fold_for_match(text: str) -> tuple[str, list[int]]:
    """只做「兼容字符折叠」，保留标点与空白，并给出偏移映射。

    与 :func:`normalize_with_map` 的区别：后者会删掉标点、空白并转小写，适合
    相似度计算；这里保留原结构，只把渲染上等价、码位不同的字符折叠成标准
    汉字/字母，供人员、报价等**基于正则的关键词抽取**使用。

    典型场景是 PDF 的 ToUnicode 表把「目、人、工」映射成康熙部首（U+2F6C
    U+2F08 U+2F2F），肉眼与打印完全一样，正则里的 ``[一-龥]`` 却匹配不到，
    导致整份 PDF 的人员重复全部漏报（QA 报告第 1 条）。

    折叠是逐字符进行的：NFKC 在同一码位上通常也是 1:1（康熙部首、兼容汉字、
    全角字母数字均如此），因此返回值与原文按下标一一对应，可以把匹配到的
    位置直接还原成原文偏移。少数会展开成多个字符的码位（如 ㈱）保持原样，
    换取映射的稳定性。
    """
    chars: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKC", char)
        if len(folded) != 1:
            folded = char
        folded = folded.translate(_HOMOGLYPHS)
        chars.append(folded)
        index_map.append(index)
    return "".join(chars), index_map


def fold(text: str) -> str:
    return fold_for_match(text)[0]


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
