"""文件级取证证据：文件指纹与文档元数据.

这两个维度和文本比对正交——改抬头、换措辞、重新排版都洗不掉：

- **文件指纹（file）**：两家企业提交的原始文件哈希完全相同。同一份文件被
  两家投标人当作自己的标书提交，是所有情形里最直接的一条线索。
- **文档元数据（metadata）**：PDF 的 Producer/Creator/Author/文档 ID、
  DOCX 的作者与最后修改人相同或高度一致，说明两份文件出自同一编写环境。

产物是 ``EvidenceDraft``，与文本/人员/价格证据共用同一条存储与审核链路，
这样风险页、三栏页和报告都不用为它单独开一套 UI。
"""

from __future__ import annotations

from app.analysis.base import EvidenceDraft
from app.models.enums import EvidenceType, RiskLevel

# 元数据字段的判定权重：直接标识编写者的字段共享时，证据强度明显高于
# 只是「生成工具相同」（同一批办公软件导出，环境相同很常见）。
_FIELD_WEIGHTS = {
    "pdf_id": 3,
    "xmp_document_id": 3,
    "xmp_instance_id": 3,
    "author": 2,
    "last_modified_by": 2,
    "company": 2,
    "producer": 1,
    "creator": 1,
}

_FIELD_LABELS = {
    "pdf_id": "PDF 文档 ID",
    "xmp_document_id": "XMP 文档 ID",
    "xmp_instance_id": "XMP 实例 ID",
    "author": "作者",
    "last_modified_by": "最后修改人",
    "company": "公司名",
    "producer": "生成工具（Producer）",
    "creator": "创建工具（Creator）",
}


def build_file_fingerprint_evidence(
    company_a: tuple[str, str],
    company_b: tuple[str, str],
    file_a: dict,
    file_b: dict,
) -> EvidenceDraft:
    """两家企业的原始文件哈希完全相同。

    ``company_x`` 为 ``(company_id, company_name)``；``file_x`` 至少包含
    ``file_id``/``file_name``/``file_hash``。
    """
    file_hash = file_a.get("file_hash") or ""
    company_a_id, company_a_name = company_a
    company_b_id, company_b_name = company_b
    summary = (
        f"{company_a_name}《{file_a['file_name']}》与 "
        f"{company_b_name}《{file_b['file_name']}》的原始文件完全相同"
        f"（SHA-256 {file_hash[:12]}…）"
    )
    return EvidenceDraft(
        evidence_type=EvidenceType.FILE.value,
        subtype="identical_file",
        # 同一份文件出现在两家投标人处，不存在「巧合」的解释空间
        severity=RiskLevel.HIGH.value,
        summary=summary,
        evidence_data={
            "file_hash": file_hash,
            "file_a_id": file_a.get("file_id"),
            "file_a_name": file_a.get("file_name"),
            "file_b_id": file_b.get("file_id"),
            "file_b_name": file_b.get("file_name"),
            "note": (
                "两份文件的字节内容完全一致。若为同一家企业代做或直接复用，"
                "属于强关联线索；也可能来自同一招标代理分发的模板，需结合实际情况核实。"
            ),
        },
        source_a=_file_source(company_a_id, company_a_name, file_a),
        source_b=_file_source(company_b_id, company_b_name, file_b),
    )


def _file_source(company_id: str, company_name: str, file: dict) -> dict:
    return {
        "company_id": company_id,
        "company_name": company_name,
        "file_id": file.get("file_id"),
        "file_name": file.get("file_name"),
        "file_hash": file.get("file_hash"),
        "locator_precision": "file",
    }


def build_metadata_evidence(
    company_a: tuple[str, str],
    company_b: tuple[str, str],
    file_a: dict,
    file_b: dict,
) -> EvidenceDraft | None:
    """两份文件的元数据指向同一编写环境或编写者。

    返回 ``None`` 表示没有足够强的共同字段——只共享 Producer 这类通用字段
    不算证据，否则同用 WPS/Office 的用户会全部互相命中。
    """
    meta_a = file_a.get("metadata") or {}
    meta_b = file_b.get("metadata") or {}
    shared: dict[str, dict] = {}
    weight = 0
    for field, field_weight in _FIELD_WEIGHTS.items():
        value_a = meta_a.get(field)
        value_b = meta_b.get(field)
        if not value_a or not value_b:
            continue
        # 工具字段区分大小写以外的差异都会误报，统一按去空格小写比较
        if str(value_a).strip().lower() != str(value_b).strip().lower():
            continue
        # 「作者为空」「作者是默认值」这类不构成证据
        if _is_placeholder(str(value_a)):
            continue
        shared[field] = {
            "value": value_a,
            "label": _FIELD_LABELS.get(field, field),
        }
        weight += field_weight

    # 只有「通用工具」字段相同（权重合计 < 2）不当证据：
    # 同一批办公软件导出的文件，Producer 相同是常态。
    if weight < 2:
        return None

    company_a_id, company_a_name = company_a
    company_b_id, company_b_name = company_b
    fields_text = "、".join(item["label"] for item in shared.values())
    summary = (
        f"{company_a_name}《{file_a['file_name']}》与 "
        f"{company_b_name}《{file_b['file_name']}》的文档元数据一致：{fields_text}"
    )
    severity = RiskLevel.HIGH.value if weight >= 4 else RiskLevel.MEDIUM.value
    return EvidenceDraft(
        evidence_type=EvidenceType.METADATA.value,
        subtype="shared_metadata",
        severity=severity,
        summary=summary,
        evidence_data={
            "shared_fields": {
                key: item["value"] for key, item in shared.items()
            },
            "shared_field_labels": {
                key: item["label"] for key, item in shared.items()
            },
            "weight": weight,
            "metadata_a": meta_a,
            "metadata_b": meta_b,
            "file_a_id": file_a.get("file_id"),
            "file_a_name": file_a.get("file_name"),
            "file_b_id": file_b.get("file_id"),
            "file_b_name": file_b.get("file_name"),
            "note": (
                "元数据相同说明两份文件可能由同一编写者或同一台设备产出。"
                "若企业共用第三方代做机构，也可能出现这种情况，需人工核实。"
            ),
        },
        source_a=_file_source(company_a_id, company_a_name, file_a),
        source_b=_file_source(company_b_id, company_b_name, file_b),
    )


# 这些值不足以标识编写者：空作者、软件默认作者、占位名
_PLACEHOLDER_VALUES = {
    "administrator",
    "admin",
    "user",
    "windows 用户",
    "microsoft office user",
    "作者",
    "unknown",
    "none",
    "null",
}


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDER_VALUES


def build_image_evidence(
    company_a: tuple[str, str],
    company_b: tuple[str, str],
    file_a: dict,
    file_b: dict,
    matches: list[dict],
) -> EvidenceDraft | None:
    """两家标书中出现同一张（或近似同一张）图片。

    印章、资质证书、业绩截图这类图片在不同投标人处重复出现，比文字雷同更难
    用「通用模板」解释。只要有一张字节完全相同就足以立为高置信线索。
    """
    if not matches:
        return None
    identical = [item for item in matches if item.get("identical")]
    severity = RiskLevel.HIGH.value if identical else RiskLevel.MEDIUM.value
    count = len(matches)
    mention = "完全相同" if identical else "高度相似"
    company_a_id, company_a_name = company_a
    company_b_id, company_b_name = company_b
    summary = (
        f"{company_a_name}《{file_a['file_name']}》与 "
        f"{company_b_name}《{file_b['file_name']}》中有 {count} 张{mention}的图片"
    )
    return EvidenceDraft(
        evidence_type=EvidenceType.METADATA.value,
        subtype="shared_image",
        severity=severity,
        summary=summary,
        evidence_data={
            "fact": "image",
            "match_count": count,
            "identical_count": len(identical),
            "images": matches[:20],
            "note": (
                "图片相同通常说明双方共用了同一份素材（印章、证书扫描件、"
                "业绩截图等）。若为常见的公章样式或公开图库素材，需人工排除。"
            ),
        },
        source_a=_file_source(company_a_id, company_a_name, file_a),
        source_b=_file_source(company_b_id, company_b_name, file_b),
    )


def build_rsid_evidence(
    company_a: tuple[str, str],
    company_b: tuple[str, str],
    file_a: dict,
    file_b: dict,
    comparison: dict,
    *,
    min_shared: int = 3,
) -> EvidenceDraft | None:
    """两份 DOCX 的修订标识交集达到门槛，说明可能来自同一底稿。"""
    shared_count = comparison.get("shared_count", 0)
    if shared_count < min_shared:
        return None
    company_a_id, company_a_name = company_a
    company_b_id, company_b_name = company_b
    trace_count = comparison.get("shared_trace_count", 0)
    summary = (
        f"{company_a_name}《{file_a['file_name']}》与 "
        f"{company_b_name}《{file_b['file_name']}》的 Word 修订标识有 "
        f"{shared_count} 个相同"
    )
    if trace_count:
        summary += f"（其中 {trace_count} 个是真实编辑痕迹）"
    severity = (
        RiskLevel.HIGH.value if trace_count >= min_shared else RiskLevel.MEDIUM.value
    )
    return EvidenceDraft(
        evidence_type=EvidenceType.METADATA.value,
        subtype="shared_rsid",
        severity=severity,
        summary=summary,
        evidence_data={
            "fact": "rsid",
            "shared_ids": comparison.get("shared_ids", [])[:50],
            "shared_count": shared_count,
            "shared_trace_count": trace_count,
            "overlap_ratio": round(comparison.get("overlap_ratio", 0.0), 4),
            "note": (
                "Word 的修订标识由每次编辑会话随机生成，只有从同一份底稿"
                "另存派生才会成批保留。改抬头、换措辞都清不掉。"
            ),
        },
        source_a=_file_source(company_a_id, company_a_name, file_a),
        source_b=_file_source(company_b_id, company_b_name, file_b),
    )
