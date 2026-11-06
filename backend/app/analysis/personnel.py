"""人员重复分析（V1 规则版）.

通过岗位关键词 + 姓名模式抽取人员，跨企业同名/近名结果作为核查线索。
注意：同名不等于同一人，系统只输出线索，需结合证件号等人工确认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.analysis.base import EvidenceDraft
from app.analysis.sources import BlockRef
from app.analysis.thresholds import personnel_thresholds
from app.models.enums import EvidenceType, RiskLevel
from app.utils.text import compact_snippet

ROLES = (
    "项目负责人",
    "项目经理",
    "技术负责人",
    "法定代表人",
    "授权代表",
    "造价人员",
    "预算员",
    "施工人员",
    "设计负责人",
    "设计师",
    "项目总监",
    "安全员",
    "质检员",
    "联系人",
    "项目组成员",
)

_ROLE_ALT = "|".join(ROLES)
_AFTER_ROLE = re.compile(
    rf"({_ROLE_ALT})(?:（[^）]{{0,12}}）)?[ \t]*(?:姓名)?[ \t]*[:：为是]?[ \t]*"
    r"([一-龥]{2,4})(?=\s|$|[,，。；;、）)（(]|\d)"
)
# 姓名必须在自然边界之后，避免把“过程中建立项目经理负责制”里的“程中建立”当成姓名
_BEFORE_ROLE = re.compile(
    r"(?<![一-龥])([一-龥]{2,4})[ \t]*[（(]?[ \t]*" rf"({_ROLE_ALT})"
)
_NAME_LABEL = re.compile(r"姓名[ \t]*[:：][ \t]*([一-龥]{2,4})(?=\s|$|[,，。；;、）)（(]|\d)")

_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)
_COMPOUND_SURNAMES = {"欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙", "慕容", "司徒"}

_STOP_WORDS = {
    "负责",
    "代表",
    "人员",
    "经理",
    "总监",
    "项目",
    "技术",
    "管理",
    "公司",
    "有限",
    "姓名",
    "联系",
    "电话",
    "地址",
    "标书",
    "文件",
    "工作",
    "以上",
    "如下",
    "详见",
    "本项",
    "投标",
    "招标",
}


@dataclass
class PersonOccurrence:
    name: str
    role: str
    block: BlockRef
    start: int
    end: int


def _valid_name(name: str) -> bool:
    if not (2 <= len(name) <= 4):
        return False
    if name in _STOP_WORDS or any(bad in name for bad in _STOP_WORDS):
        return False
    if name[:2] in _COMPOUND_SURNAMES:
        return len(name) >= 3
    return name[0] in _SURNAMES


def extract_people(blocks: list[BlockRef]) -> list[PersonOccurrence]:
    occurrences: list[PersonOccurrence] = []
    seen: set[tuple[str, str, str]] = set()
    for block in blocks:
        for match in _AFTER_ROLE.finditer(block.content):
            role, name = match.group(1), match.group(2)
            if not _valid_name(name):
                continue
            key = (name, role, block.block_id)
            if key in seen:
                continue
            seen.add(key)
            occurrences.append(
                PersonOccurrence(name, role, block, match.start(2), match.end(2))
            )
        for match in _BEFORE_ROLE.finditer(block.content):
            name, role = match.group(1), match.group(2)
            if not _valid_name(name):
                continue
            key = (name, role, block.block_id)
            if key in seen:
                continue
            seen.add(key)
            occurrences.append(
                PersonOccurrence(name, role, block, match.start(1), match.end(1))
            )
        for match in _NAME_LABEL.finditer(block.content):
            name = match.group(1)
            if not _valid_name(name):
                continue
            key = (name, "未标注岗位", block.block_id)
            if key in seen:
                continue
            seen.add(key)
            occurrences.append(
                PersonOccurrence(name, "未标注岗位", block, match.start(1), match.end(1))
            )
    return occurrences


def _source(occ: PersonOccurrence) -> dict:
    return occ.block.source(occ.start, occ.end)


def _occurrence_payload(occ: PersonOccurrence) -> dict:
    return {
        "name": occ.name,
        "role": occ.role,
        "company_name": occ.block.company_name,
        "file_id": occ.block.file_id,
        "file_name": occ.block.file_name,
        "page_index": occ.block.page_index,
        "printed_page_number": occ.block.printed_page_number,
        "block_id": occ.block.block_id,
        "snippet": compact_snippet(occ.block.content, 200),
    }


def build_personnel_evidences(
    occurrences_a: list[PersonOccurrence],
    occurrences_b: list[PersonOccurrence],
    *,
    thresholds: dict | None = None,
) -> list[EvidenceDraft]:
    rules = personnel_thresholds(thresholds)
    by_name_a: dict[str, list[PersonOccurrence]] = {}
    by_name_b: dict[str, list[PersonOccurrence]] = {}
    for occ in occurrences_a:
        by_name_a.setdefault(occ.name, []).append(occ)
    for occ in occurrences_b:
        by_name_b.setdefault(occ.name, []).append(occ)

    drafts: list[EvidenceDraft] = []
    exact_names = set(by_name_a) & set(by_name_b)
    for name in sorted(exact_names):
        a_list, b_list = by_name_a[name], by_name_b[name]
        company_a = a_list[0].block.company_name
        company_b = b_list[0].block.company_name
        roles = sorted({o.role for o in a_list + b_list})
        severity = (
            RiskLevel.HIGH.value
            if len(a_list) + len(b_list) >= 4
            else RiskLevel.MEDIUM.value
        )
        drafts.append(
            EvidenceDraft(
                evidence_type=EvidenceType.PERSONNEL.value,
                subtype="exact_name",
                severity=severity,
                summary=(
                    f"人员姓名“{name}”同时出现在 {company_a} 与 {company_b}，"
                    f"涉及岗位：{'、'.join(roles)}"
                ),
                evidence_data={
                    "name": name,
                    "match_type": "exact",
                    "roles": roles,
                    "occurrences_a": [_occurrence_payload(o) for o in a_list],
                    "occurrences_b": [_occurrence_payload(o) for o in b_list],
                    "note": "同名不代表同一人，需要结合证件号码、资格证书等进一步核实",
                },
                source_a=_source(a_list[0]),
                source_b=_source(b_list[0]),
            )
        )

    # 疑似相似姓名（例如“李娜”/“李那”），作为低风险线索保留
    seen_pairs: set[frozenset[str]] = set()
    for name_a, a_list in by_name_a.items():
        for name_b, b_list in by_name_b.items():
            if name_a == name_b:
                continue
            if len(name_a) < 3 or len(name_b) < 3:
                continue
            pair_key = frozenset((name_a, name_b))
            if pair_key in seen_pairs:
                continue
            ratio = SequenceMatcher(None, name_a, name_b).ratio()
            if ratio < rules["similar_name_ratio"]:
                continue
            seen_pairs.add(pair_key)
            drafts.append(
                EvidenceDraft(
                    evidence_type=EvidenceType.PERSONNEL.value,
                    subtype="similar_name",
                    severity=RiskLevel.LOW.value,
                    summary=(
                        f"疑似相似姓名“{name_a}”与“{name_b}”分别出现在 "
                        f"{a_list[0].block.company_name} 与 {b_list[0].block.company_name}"
                    ),
                    evidence_data={
                        "name_a": name_a,
                        "name_b": name_b,
                        "match_type": "similar",
                        "similarity": round(ratio, 3),
                        "occurrences_a": [_occurrence_payload(o) for o in a_list],
                        "occurrences_b": [_occurrence_payload(o) for o in b_list],
                    },
                    source_a=_source(a_list[0]),
                    source_b=_source(b_list[0]),
                )
            )
    return drafts
