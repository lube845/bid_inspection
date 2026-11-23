"""单家企业价格 Excel 的解析与导入.

文件管理里每家企业既可以上传标书，也可以上传一份**价格 Excel**。它的语义是
「这家企业的权威报价资料」，因此优先级高于正文自动识别：

    价格 Excel（import） > 手工补录（manual） > 正文自动识别（auto）

Excel 支持两种表格：

1. **清单 + 合计**（推荐）：表头含「项目名称 / 单价」，按行导入分项单价；
   若同时有「合计」行或「投标总报价」行，一并写入总价。
2. **只有总价**：表头含「投标总报价（元）」，等价于原来的报价导入。

表头识别是宽松的：中英文、全半角、带不带单位都能认，找不到就按列位置兜底，
避免各家模板不同导致整份文件白传。
"""

from __future__ import annotations

import csv
import io
import re

from openpyxl import load_workbook

from app.services.quote_service import QuoteError, _clean_amount

# 列名别名：统一小写并去掉括号里的单位后再匹配
_NAME_KEYS = ("项目名称", "清单项目", "分项名称", "名称", "item", "项目")
_PRICE_KEYS = ("单价", "综合单价", "unitprice", "unit price")
_UNIT_KEYS = ("单位", "unit")
_QTY_KEYS = ("数量", "工程量", "quantity", "qty")
_TOTAL_KEYS = ("投标总报价", "总报价", "投标报价", "总价", "合计金额", "total")
_UPPER_KEYS = ("大写金额", "大写")

_HEADER_SCAN_ROWS = 10
_MAX_ITEMS = 2000


def _norm_header(cell) -> str:
    text = str(cell or "").strip().lower()
    text = re.sub(r"[（(].*?[）)]", "", text)
    return re.sub(r"[\s_\-:：]+", "", text)


def _match_column(header: list[str], keys: tuple[str, ...]) -> int | None:
    for index, name in enumerate(header):
        if not name:
            continue
        for key in keys:
            normalized = _norm_header(key)
            if normalized and (normalized in name or name in normalized):
                return index
    return None


def _read_rows(filename: str, data: bytes) -> list[list]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix in {"xlsx", "xlsm"}:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        rows = []
        # 单企业价格文件通常只有一张表，但模板里可能带「说明」页，全部扫一遍
        for sheet in wb.worksheets:
            rows.extend([list(row) for row in sheet.iter_rows(values_only=True)])
        return rows
    if suffix == "csv":
        text = None
        for encoding in ("utf-8-sig", "gb18030", "utf-8"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise QuoteError("CSV 文件编码无法识别，请另存为 UTF-8 或 xlsx")
        return [row for row in csv.reader(io.StringIO(text))]
    raise QuoteError("价格文件仅支持 .xlsx / .csv 格式")


def parse_company_price_file(filename: str, data: bytes) -> dict:
    """解析单家企业的价格文件，返回总价 + 分项单价（不落库）。

    返回结构::

        {
          "total_amount": float | None,
          "amount_upper": str | None,
          "items": [{"name", "key", "unit", "quantity", "unit_price", "row"}],
          "errors": [...], "warnings": [...],
        }
    """
    rows = _read_rows(filename, data)
    if not rows:
        raise QuoteError("价格文件内容为空")

    header_index, columns = _locate_header(rows)
    if not columns:
        raise QuoteError(
            "未能识别表头：请包含「项目名称」与「单价」列，或「投标总报价（元）」列"
        )

    total_amount: float | None = None
    amount_upper: str | None = None
    items: list[dict] = []
    errors: list[dict] = []
    warnings: list[dict] = []
    seen_keys: set[str] = set()

    for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(str(cell or "").strip() for cell in row):
            continue

        def cell(index: int | None) -> str:
            if index is None or index >= len(row):
                return ""
            return str(row[index] or "").strip()

        name = cell(columns.get("name"))
        price_text = cell(columns.get("price"))
        total_text = cell(columns.get("total"))

        # 合计行 / 总报价行：表格里常见「合计」写在名称列、金额写在最后一列，
        # 此时单价列是空的。先认出这类行，避免把「合计」当成一个清单项。
        if total_text:
            candidate = _clean_amount(total_text)
            if candidate and candidate > 0:
                total_amount = candidate
                if columns.get("upper"):
                    amount_upper = cell(columns.get("upper")) or None
                continue
        if _looks_total_row(name) and not price_text:
            # 名称列是「合计/总计」，金额取整行最后一个能解析成金额的单元格
            for value in reversed(row):
                candidate = _clean_amount(value)
                if candidate and candidate > 0:
                    total_amount = candidate
                    break
            continue

        if not name or not price_text:
            continue
        price = _clean_amount(price_text)
        if price is None or price <= 0:
            errors.append(
                {"row": offset, "message": f"单价无法解析：{price_text or '（空）'}"}
            )
            continue
        from app.utils.text import normalize

        key = normalize(name)
        if len(key) < 2:
            continue
        if key in seen_keys:
            warnings.append({"row": offset, "message": f"清单项「{name}」重复，已保留第一行"})
            continue
        seen_keys.add(key)
        if len(items) >= _MAX_ITEMS:
            warnings.append(
                {"row": offset, "message": f"分项超过 {_MAX_ITEMS} 条，后续行已忽略"}
            )
            break
        quantity = _clean_amount(cell(columns.get("quantity")))
        items.append(
            {
                "row": offset,
                "name": name,
                "key": key,
                "unit": cell(columns.get("unit")) or None,
                "quantity": quantity if quantity and quantity > 0 else None,
                "unit_price": round(price, 4),
            }
        )

    if total_amount is None and items:
        # 没有合计行时，用「数量 × 单价」求和作为提示值，但明确标注是推算的
        computable = [
            item["unit_price"] * item["quantity"]
            for item in items
            if item.get("quantity")
        ]
        if computable and len(computable) == len(items):
            total_amount = round(sum(computable), 2)
            warnings.append(
                {"row": 0, "message": f"文件未写总报价，已按「数量 × 单价」推算为 {total_amount:,.2f} 元"}
            )

    if total_amount is None and not items:
        raise QuoteError("没有解析到任何总报价或分项单价，请检查表格结构")
    if not items:
        warnings.append({"row": 0, "message": "文件只有总报价，没有分项清单：价格矩阵将无法比较逐项单价"})

    return {
        "total_amount": total_amount,
        "amount_upper": amount_upper,
        "items": items,
        "errors": errors,
        "warnings": warnings,
        "header_row": header_index + 1,
    }


def _locate_header(rows: list[list]) -> tuple[int, dict[str, int]]:
    """在前若干行里找表头，返回（行下标, 列映射）。"""
    for index, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        header = [_norm_header(cell) for cell in row]
        if not any(header):
            continue
        columns = {
            "name": _match_column(header, _NAME_KEYS),
            "price": _match_column(header, _PRICE_KEYS),
            "unit": _match_column(header, _UNIT_KEYS),
            "quantity": _match_column(header, _QTY_KEYS),
            "total": _match_column(header, _TOTAL_KEYS),
            "upper": _match_column(header, _UPPER_KEYS),
        }
        has_items = columns["name"] is not None and columns["price"] is not None
        has_total = columns["total"] is not None
        if has_items or has_total:
            return index, columns
    return 0, {}


def _looks_total_row(name: str) -> bool:
    return bool(re.search(r"合计|总计|总价|总报价", name or ""))
