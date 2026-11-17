"""DOCX 修订标识符（rsid）取证.

Word 在文档里为每一次编辑会话分配修订标识符（``w:rsid*``，8 位十六进制），
写在 ``word/settings.xml`` 的 ``<w:rsid>`` 列表和 ``word/document.xml`` 的
元素属性上。这些 ID 由 Word 随机生成、与实际编辑操作绑定：

- 同一份底稿在 Word 里「另存为」成多份文件，rsid 会**成批保留**；
- 两家企业各自从零起草，rsid 交集几乎为空。

因此「两份标书的 rsid 交集非空、且重叠数量可观」是同一底稿派生的强线索，
改抬头、换措辞、重排版都不影响它——只有「复制粘贴到新文档」才会丢失大部分。

判定门槛：
- 交集至少 ``min_shared`` 个（默认 3）：单个 ID 可能是巧合；
- 交集中至少有一个是「编辑会话」级 ID（出现在 ``document.xml`` 里而不只是
  settings 的列表），说明是真实编辑痕迹而非模板残留；
- 必须先扣除**批次共性 ID**（见 ``analysis_service._batch_common_rsid``）。

已知限制：程序化生成的 DOCX（python-docx 等）会给每份文档写入同一套固定
rsid（实测互不相关的三份文件交集是全部 9 个）。批次共性扣除会把这类文档的
rsid 证据全部消掉——这是有意的保守行为：宁可对程序生成件不报，也不能让
每一对企业都被误判同源。真实 Word 编辑产生的 rsid 随会话随机，不受影响。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass

_RSID_ATTR = re.compile(r'w:rsid\w*="([0-9A-Fa-f]{8})"')
_RSID_SETTING = re.compile(r'<w:rsid\b[^>]*w:val="([0-9A-Fa-f]{8})"')


@dataclass
class RsidProfile:
    """一份 DOCX 的修订标识集合。"""

    all_ids: set[str]
    trace_ids: set[str]

    def summary(self) -> dict:
        return {
            "count": len(self.all_ids),
            "trace_count": len(self.trace_ids),
        }


def extract_rsid(path) -> RsidProfile | None:
    """读取 DOCX 的 rsid 集合；非 DOCX 或读取失败返回 None。"""
    try:
        with zipfile.ZipFile(str(path)) as archive:
            names = archive.namelist()
            if "word/document.xml" not in names:
                return None
            document = archive.read("word/document.xml").decode("utf-8", "ignore")
            settings = ""
            if "word/settings.xml" in names:
                settings = archive.read("word/settings.xml").decode("utf-8", "ignore")
    except Exception:
        return None

    # document.xml 里的属性是真实编辑痕迹，settings 里的 rsid 列表是会话清单
    trace_ids = {value.upper() for value in _RSID_ATTR.findall(document)}
    all_ids = trace_ids | {value.upper() for value in _RSID_SETTING.findall(settings)}
    return RsidProfile(all_ids=all_ids, trace_ids=trace_ids)


def compare_rsid(profile_a: RsidProfile, profile_b: RsidProfile) -> dict:
    """计算两份文档的 rsid 交集及其中的真实编辑痕迹。"""
    shared = profile_a.all_ids & profile_b.all_ids
    shared_trace = profile_a.trace_ids & profile_b.trace_ids
    return {
        "shared_ids": sorted(shared),
        "shared_count": len(shared),
        "shared_trace": sorted(shared_trace),
        "shared_trace_count": len(shared_trace),
        "overlap_ratio": (
            len(shared) / min(len(profile_a.all_ids), len(profile_b.all_ids))
            if profile_a.all_ids and profile_b.all_ids
            else 0.0
        ),
    }
