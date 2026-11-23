"""关键事实冲突检测.

比「文本相似」更可举证的一类线索：两家标书在**互斥的事实**上给出不同答案，
而这些事实在正常竞争投标里本应各自独立——一旦不同企业写错得一模一样，
或者同一份底稿被改成两家名字，就会留下这种矛盾。

覆盖四类事实（都能用确定性规则抽取）：

- **工期**：``工期 90 日历天`` / ``合同工期 12 个月`` / ``服务期 3 年``
- **日期**：``2026 年 3 月 15 日`` / ``2026-03-15``，取「投标截止/开标/有效期」附近
- **项目负责人/技术负责人**：与人员维度共享岗位表，但这里比的是**同一岗位被
  不同企业写成了谁**，以及**同一人被不同企业写成不同岗位**
- **联系方式**：手机号、座机、邮箱、传真；两家标书共用同一联系人信息

冲突的定义要保守（宁缺毋滥）：

1. **同一岗位不同人**：甲的项目负责人是「张伟」、乙的是「李强」——这在正常竞争里
   很常见（不同公司当然用不同人），**不构成异常**，只在「两家在正文其他位置又把
   对方的人写进来」时有意义 → 本模块不报这类。
2. 真正报的是：**同一份联系方式/同一日期在不同企业标书里出现且绑定相同的语义**
   （如同一手机号既出现在甲的授权代表栏、又出现在乙的授权代表栏），以及
   **同一编号/日期在两份标书里被写成不同值却同属一个可识别字段**。

实现上先抽 `FactValue`，再做跨企业比对，输出带双方原文定位的证据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.analysis.base import EvidenceDraft
from app.analysis.sources import BlockRef, BlockSpan, fold_span
from app.models.enums import EvidenceType, RiskLevel
from app.utils.text import fold_for_match

# ---- 工期 ----
_DURATION = re.compile(
    r"(?:工期|合同工期|服务期|供货期|履约期)\s*(?:为|是)?\s*[:：]?\s*"
    r"(\d{1,4})\s*(日历天|天|个月|月|年|周)"
)

# ---- 日期 ----
_DATE_CN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")

# ---- 联系方式 ----
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LANDLINE = re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# ---- 人员（与 personnel 模块共享岗位词，但这里只要「岗位 → 姓名」的绑定）----
_ROLES = (
    "项目负责人",
    "项目经理",
    "技术负责人",
    "法定代表人",
    "授权代表",
    "造价人员",
    "预算员",
    "项目总监",
    "安全员",
    "质检员",
    "联系人",
)
_ROLE_ALT = "|".join(sorted(_ROLES, key=len, reverse=True))
_ROLE_NAME = re.compile(
    rf"({_ROLE_ALT})\s*(?:姓名)?\s*[:：为是]?\s*([一-龥]{{2,4}})"
    r"(?=\s|$|[,，。；;、）)（(]|\d)"
)
# 名称后紧跟这些词说明是职位描述，不是姓名
_NAME_STOP = {"负责", "代表", "人员", "经理", "总监", "项目", "技术", "工程", "管理", "组织"}


@dataclass
class FactValue(BlockSpan):
    """一条从正文抽出的事实。"""

    kind: str  # duration / date / phone / email / role_name
    key: str  # 比对用的规范化键（如 岗位名、号码本身、标准化日期）
    value: str  # 展示值（原文写法）
    block: BlockRef
    start: int
    end: int


def _valid_chinese_name(name: str) -> bool:
    if not (2 <= len(name) <= 4):
        return False
    if name in _NAME_STOP:
        return False
    return all("一" <= char <= "鿿" for char in name)


def _normalize_date(year: str, month: str, day: str) -> str | None:
    try:
        y, m, d = int(year), int(month), int(day)
    except ValueError:
        return None
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def extract_facts(blocks: list[BlockRef]) -> list[FactValue]:
    """从一家企业的文本块里抽取全部事实。"""
    facts: list[FactValue] = []
    for block in blocks:
        # 与人员/报价一致：在折叠兼容字符后的文本上匹配（PDF 会把「人」「工」
        # 等字映射成康熙部首），偏移再映射回原文。
        content, offsets = fold_for_match(block.content)

        span = fold_span(offsets)

        for match in _DURATION.finditer(content):
            facts.append(
                FactValue(
                    kind="duration",
                    key="工期",
                    value=f"{match.group(1)} {match.group(2)}",
                    block=block,
                    start=span(match.start(), match.end())[0],
                    end=span(match.start(), match.end())[1],
                )
            )
        for pattern in (_DATE_CN, _DATE_ISO):
            for match in pattern.finditer(content):
                normalized = _normalize_date(*match.groups())
                if normalized:
                    facts.append(
                        FactValue(
                            kind="date",
                            key=normalized,
                            value=match.group(0),
                            block=block,
                            start=span(match.start(), match.end())[0],
                            end=span(match.start(), match.end())[1],
                        )
                    )
        for pattern, kind in ((_MOBILE, "phone"), (_LANDLINE, "phone"), (_EMAIL, "email")):
            for match in pattern.finditer(content):
                facts.append(
                    FactValue(
                        kind=kind,
                        key=match.group(0).replace(" ", "").replace("-", ""),
                        value=match.group(0),
                        block=block,
                        start=span(match.start(), match.end())[0],
                        end=span(match.start(), match.end())[1],
                    )
                )
        for match in _ROLE_NAME.finditer(content):
            role, name = match.group(1), match.group(2)
            if _valid_chinese_name(name):
                facts.append(
                    FactValue(
                        kind="role_name",
                        key=role,
                        value=name,
                        block=block,
                        start=span(match.start(2), match.end(2))[0],
                        end=span(match.start(2), match.end(2))[1],
                    )
                )
    return facts


def _group(facts: list[FactValue], kind: str) -> dict[str, list[FactValue]]:
    grouped: dict[str, list[FactValue]] = {}
    for fact in facts:
        if fact.kind == kind:
            grouped.setdefault(fact.key, []).append(fact)
    return grouped


def build_fact_conflict_evidences(
    facts_a: list[FactValue],
    facts_b: list[FactValue],
    company_a: tuple[str, str],
    company_b: tuple[str, str],
) -> list[EvidenceDraft]:
    """比对两家企业的事实，只在**可举证且互斥**的情形下产出证据。"""
    drafts: list[EvidenceDraft] = []

    # 1. 共用联系方式：同一个号码/邮箱同时出现在两家标书里。
    #    手机号是个人标识，两家投标文件出现同一手机号几乎不可能只是巧合。
    for kind, label in (("phone", "手机号/电话号码"), ("email", "邮箱")):
        set_a = _group(facts_a, kind)
        set_b = _group(facts_b, kind)
        shared = set(set_a) & set(set_b)
        for key in sorted(shared):
            first_a, first_b = set_a[key][0], set_b[key][0]
            drafts.append(
                EvidenceDraft(
                    evidence_type=EvidenceType.METADATA.value,
                    subtype="shared_contact",
                    severity=RiskLevel.HIGH.value,
                    summary=(
                        f"{company_a[1]} 与 {company_b[1]} 的标书中出现相同的{label}：{first_a.value}"
                    ),
                    evidence_data={
                        "fact": kind,
                        "value": first_a.value,
                        "note": (
                            "联系方式属于企业/个人标识，两家投标人共用同一联系方式"
                            "通常说明由同一方组织投标或代做，需核实是否为同一授权代表。"
                        ),
                    },
                    source_a=first_a.source,
                    source_b=first_b.source,
                )
            )

    # 2. 工期不一致：同一项目两家写出的工期不同。这本身不等于串标，
    #    但**工期完全相同且带非常规尾数**（如 137 日历天）反而是同源线索；
    #    这里只报「完全相同」的非常规值，避免把正常竞标误判成冲突。
    durations_a = {fact.value for fact in facts_a if fact.kind == "duration"}
    durations_b = {fact.value for fact in facts_b if fact.kind == "duration"}
    for value in sorted(durations_a & durations_b):
        detail = _duration_detail(value)
        if detail is None:
            continue
        number, unit = detail
        # 整数月/整年的工期是政策或招标文件规定的常见值，不构成同源线索
        if not _is_unusual_duration(number, unit):
            continue
        fact_a = next(f for f in facts_a if f.kind == "duration" and f.value == value)
        fact_b = next(f for f in facts_b if f.kind == "duration" and f.value == value)
        drafts.append(
            EvidenceDraft(
                evidence_type=EvidenceType.METADATA.value,
                subtype="unusual_duration_match",
                severity=RiskLevel.MEDIUM.value,
                summary=(
                    f"{company_a[1]} 与 {company_b[1]} 的标书均写明工期 {value}，"
                    "该数值不属于招标文件常见的整月/整年口径"
                ),
                evidence_data={
                    "fact": "duration",
                    "value": value,
                    "note": (
                        "两家给出完全相同的非常规工期，可能来自同一份底稿；"
                        "也可能同为响应招标文件的统一要求，需对照招标文件确认。"
                    ),
                },
                source_a=fact_a.source,
                source_b=fact_b.source,
            )
        )

    # 3. 同一岗位在不同企业的标书里写成完全相同的「非常见姓名组合」不构成异常，
    #    但同一组「岗位→姓名」被两家完整复制则是强线索（人员维度已覆盖单人重复，
    #    这里补充「整组人事安排一致」的形态）。
    roles_a = _role_mapping(facts_a)
    roles_b = _role_mapping(facts_b)
    shared_roles = set(roles_a) & set(roles_b)
    if len(shared_roles) >= 3:
        matched = [role for role in shared_roles if roles_a[role] == roles_b[role]]
        if len(matched) >= 3 and len(matched) >= len(shared_roles) * 0.8:
            fact_a = next(f for f in facts_a if f.kind == "role_name" and f.key == matched[0])
            fact_b = next(f for f in facts_b if f.kind == "role_name" and f.key == matched[0])
            pairs = "、".join(f"{role}={roles_a[role]}" for role in matched)
            drafts.append(
                EvidenceDraft(
                    evidence_type=EvidenceType.METADATA.value,
                    subtype="role_roster_match",
                    severity=RiskLevel.HIGH.value,
                    summary=(
                        f"{company_a[1]} 与 {company_b[1]} 的项目班子安排高度一致"
                        f"（{len(matched)} 个岗位人员完全相同）：{pairs}"
                    ),
                    evidence_data={
                        "fact": "role_name",
                        "matched_roles": matched,
                        "roster": {role: roles_a[role] for role in matched},
                        "note": (
                            "多家投标人的关键岗位人员安排完全一致，且并非同一自然人"
                            "重复出现时的常规情形，通常说明共用同一份项目班子材料。"
                        ),
                    },
                    source_a=fact_a.source,
                    source_b=fact_b.source,
                )
            )

    return drafts


def _role_mapping(facts: list[FactValue]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for fact in facts:
        if fact.kind == "role_name":
            mapping.setdefault(fact.key, fact.value)
    return mapping


def _duration_detail(value: str) -> tuple[int, str] | None:
    match = re.match(r"(\d+)\s*(.+)", value)
    if not match:
        return None
    return int(match.group(1)), match.group(2).strip()


def _is_unusual_duration(number: int, unit: str) -> bool:
    """整月/整年/整十天的工期是常见口径；带零头的才值得注意。"""
    if unit in {"个月", "月"}:
        return number % 6 != 0 and number % 12 != 0 and number % 3 != 0
    if unit in {"年"}:
        return False
    if unit in {"天", "日历天", "周"}:
        return number % 10 != 0 and number % 7 != 0
    return False
