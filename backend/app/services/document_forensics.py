"""文件级取证附属信息的聚合入口.

解析文件时一次性算好、随 ``OriginalFile.doc_metadata`` 落库：

- ``images``：内嵌图片的感知哈希列表（同一张图 / 同一枚印章的直接证据）；
- ``rsid``：DOCX 的修订标识计数与集合（同一底稿派生的证据）。

放在独立模块里，是为了让 ``file_service`` 不必直接依赖 Pillow 与 zipfile 的细节，
也方便在依赖缺失（精简环境没有 Pillow）时整体降级而不抛出异常。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.analysis import image_hash, rsid

logger = logging.getLogger(__name__)

# 单份文件最多保留的图片指纹条数：几百页扫描标书会产生上千张图，
# 全部入库会让 evidence_data 膨胀；取证只需要足够多的样本做比对。
# 同时它也是解析耗时的上限——每张图约 30ms，120 张约 4 秒。
MAX_IMAGES = 120


def build_metadata_extras(path: Path, file_type: str) -> dict:
    """返回要并入 ``doc_metadata`` 的取证字段。"""
    extras: dict = {}
    try:
        images = image_hash.build_image_fingerprints(path, file_type)
        if images:
            extras["images"] = images[:MAX_IMAGES]
            extras["image_count"] = len(images)
    except Exception as exc:  # pragma: no cover - 图片解析异常不应阻断解析
        logger.info("图片指纹提取失败 %s: %s", path.name, exc)

    if (file_type or "").lower().lstrip(".") in {"docx", "wps"}:
        try:
            profile = rsid.extract_rsid(path)
        except Exception as exc:  # pragma: no cover
            logger.info("rsid 提取失败 %s: %s", path.name, exc)
            profile = None
        if profile is not None:
            extras["rsid_ids"] = sorted(profile.all_ids)
            extras["rsid_trace_ids"] = sorted(profile.trace_ids)
    return extras
