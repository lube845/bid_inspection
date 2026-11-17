"""统一社会信用代码校验（GB 32100-2015）。

18 位代码由登记管理部门代码、机构类别代码、登记管理机关行政区划码、主体
标识码（组织机构代码）和校验码组成，字符集为
``0123456789ABCDEFGHJKLMNPQRTUWXY``（不含 I、O、S、V、Z）。

校验位算法：前 17 位分别乘以权重
``[1,3,9,27,19,26,16,17,20,29,25,13,8,24,10,30,28]``，求和后取模 31，
``31 - (和 mod 31)`` 再对 31 取模即为校验字符。

调用方应把「格式/校验位不通过」与「允许外国企业等其他证件」区分开：本模块
只负责前者，后者由 schema 的证件类型字段显式放行，而不是静默跳过校验。
"""

from __future__ import annotations

CREDIT_CODE_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
CREDIT_CODE_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
CREDIT_CODE_LENGTH = 18


def normalize_credit_code(value: str | None) -> str | None:
    """去空白、转大写；空值返回 None。"""
    if value is None:
        return None
    cleaned = "".join(str(value).split()).upper()
    return cleaned or None


def credit_code_error(value: str | None) -> str | None:
    """返回不适合作为统一社会信用代码的原因；合法时返回 None。

    只做确定性的格式与校验位判断，不查询登记机关数据库。
    """
    code = normalize_credit_code(value)
    if code is None:
        return None
    if len(code) != CREDIT_CODE_LENGTH:
        return f"统一社会信用代码应为 18 位，当前为 {len(code)} 位"
    invalid = sorted({char for char in code if char not in CREDIT_CODE_CHARS})
    if invalid:
        return (
            "统一社会信用代码包含非法字符 "
            + "、".join(invalid)
            + "（不含 I、O、S、V、Z）"
        )
    total = sum(
        CREDIT_CODE_CHARS.index(char) * weight
        for char, weight in zip(code[:17], CREDIT_CODE_WEIGHTS)
    )
    expected = CREDIT_CODE_CHARS[(31 - total % 31) % 31]
    if code[-1] != expected:
        return f"统一社会信用代码校验位不正确（末位应为 {expected}）"
    return None
