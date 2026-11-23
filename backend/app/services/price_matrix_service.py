"""价格比对矩阵（项目内可视化）.

报价清单回答「每家报了多少」，但围标串标最有说服力的线索在**逐项单价**上：
两家在不同分项上报出同一个价格、或同一个人算错同一个数，都只可能来自同一份底稿。

这个服务把「企业 × 清单项」拼成一张矩阵，并为每个单元格算出可解释的标记：

- ``identical``：与其他至少一家企业的单价完全相同（保留同样的两位小数）；
- ``near``：与至少一家的单价偏差在阈值内（例如都算成 1.2345 元）；
- ``outlier``：与「同一清单项的多家中位价」偏差显著（偏离市场/同行价）；
- ``peer``：有可比的同行价但落在正常区间；
- ``single``：只有这一家报了这个分项，无法横向比较。

所有阈值都复用价格维度的规则版本（``price_line_item_identical_tolerance``、
``price_matrix_outlier_ratio``），不另起一套口径。

数据来源是最新分析任务的文本块（与证据识别同源），因此「矩阵里看到的一致」
与「证据里报的雷同」永远是同一份数据，不会出现两套结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.line_items import extract_line_items
from app.models import AnalysisTask, Company, Project
from app.services import quote_service, task_scope


@dataclass
class _Cell:
    """一家企业在某个清单项上的单价。"""

    company_id: str
    amount: float
    source: dict
    raw_name: str


def _company_order(db: Session, project: Project) -> list[Company]:
    return list(
        db.scalars(
            select(Company)
            .where(Company.project_id == project.id, Company.deleted_at.is_(None))
            .order_by(Company.sort_order, Company.created_at)
        )
    )


def _task_blocks(db: Session, task: AnalysisTask) -> list[dict]:
    """按企业加载任务快照里的文本块（与证据识别同一份输入）。"""
    from app.services.analysis_service import _blocks_for_snapshot

    companies = (task.company_snapshot or {}).get("companies", [])
    return [
        {"company_id": company["company_id"], "blocks": _blocks_for_snapshot(db, company)}
        for company in companies
    ]


def _cell_payload(cell: _Cell, *, status: str, peers: list[float]) -> dict:
    return {
        "company_id": cell.company_id,
        "amount": round(cell.amount, 4),
        "status": status,
        # 同项其他企业的价格（去重、升序），前端悬浮提示直接展示
        "peers": sorted({round(value, 4) for value in peers}),
        "source": cell.source,
        "raw_name": cell.raw_name,
    }


def build_price_matrix(db: Session, project: Project) -> dict:
    """组装「企业 × 清单项」的价格矩阵。

    没有分析任务时返回空矩阵并给出提示，由前端引导先跑一次分析——逐项单价
    只能从已解析的正文里读，报价清单页录入的是总价，两类数据不同源。
    """
    task_id = task_scope.latest_task_id(db, project.id)
    task = db.get(AnalysisTask, task_id) if task_id else None

    companies = _company_order(db, project)
    imported = quote_service.list_price_items(db, project)
    if task is None and not imported:
        return {
            "task": None,
            "companies": [
                _company_payload(db, company, None) for company in companies
            ],
            "rows": [],
            "summary": {"row_count": 0, "cell_count": 0},
            "reason": (
                "还没有可比较的价格：请在「文件管理」为企业上传价格 Excel（含清单分项），"
                "或先完成一次分析让系统从正文识别。"
            ),
        }
    names = {company.id: company.company_name for company in companies}
    ordered_ids = [company.id for company in companies]

    # 分项单价来源优先级：价格 Excel（PriceItem）> 正文自动识别。
    # 上传过价格文件的企业以文件为准，没有再回退到正文解析——这正是
    # 「如果上传了企业价格信息 excel 就以 excel 为准」的落地位置。
    imported_by_company = quote_service.list_price_items(db, project)
    cells_source: dict[str, dict[str, _Cell]] = {}
    for company_id, rows in imported_by_company.items():
        if company_id not in names:
            continue
        cells_source[company_id] = {
            row["key"]: _Cell(
                company_id=company_id,
                amount=float(row["unit_price"]),
                source={
                    "locator_precision": "price_file",
                    "source_kind": "import",
                    "file_name": (row.get("source_ref") or {}).get("filename") or "价格文件",
                    "raw_name": row["name"],
                },
                raw_name=row["name"],
            )
            for row in rows
        }
    for entry in _task_blocks(db, task) if task is not None else []:
        company_id = entry["company_id"]
        if company_id not in names or company_id in cells_source:
            continue
        items = extract_line_items(entry["blocks"])
        if items:
            cells_source[company_id] = {
                key: _Cell(
                    company_id=company_id,
                    amount=float(item.unit_price),
                    source=item.source,
                    raw_name=item.raw_name,
                )
                for key, item in items.items()
            }

    thresholds = _price_rules(task) if task is not None else {
        "identical_tolerance": 0.0001,
        "outlier_ratio": 0.3,
    }
    identical_tolerance = float(thresholds.get("identical_tolerance", 0.0001))
    outlier_ratio = float(thresholds.get("outlier_ratio", 0.3))

    # 以「规范化项目名」为行键；展示名取第一个遇到的原名
    row_keys: dict[str, str] = {}
    for company_id in ordered_ids:
        for key, cell in cells_source.get(company_id, {}).items():
            row_keys.setdefault(key, cell.raw_name)

    rows = []
    cell_count = 0
    flagged_cells = 0
    identical_cells = 0
    for key, display_name in row_keys.items():
        cells: dict[str, _Cell] = {}
        for company_id in ordered_ids:
            cell = cells_source.get(company_id, {}).get(key)
            if cell is None or cell.amount <= 0:
                continue
            cells[company_id] = cell
        if not cells:
            continue

        amounts = [cell.amount for cell in cells.values()]
        row_median = median(amounts) if amounts else 0.0
        row_payload = {
            "key": key,
            "name": display_name,
            "peer_count": len(amounts),
            "median": round(row_median, 4) if amounts else None,
            "min": round(min(amounts), 4) if amounts else None,
            "max": round(max(amounts), 4) if amounts else None,
            "cells": [],
        }

        for company_id in ordered_ids:
            cell = cells.get(company_id)
            if cell is None:
                row_payload["cells"].append(
                    {
                        "company_id": company_id,
                        "amount": None,
                        "status": "missing",
                        "peers": [],
                        "source": None,
                        "raw_name": "",
                    }
                )
                continue
            cell_count += 1
            peers = [other.amount for other in cells.values() if other is not cell]
            status = _classify(
                cell.amount,
                peers,
                identical_tolerance=identical_tolerance,
                outlier_ratio=outlier_ratio,
            )
            if status in {"identical", "near"}:
                identical_cells += 1
            if status in {"identical", "near", "outlier"}:
                flagged_cells += 1
            row_payload["cells"].append(
                _cell_payload(cell, status=status, peers=peers)
            )
        rows.append(row_payload)

    # 有最多企业共同参与的分项排在前面：这些行的横向可比性最强
    rows.sort(key=lambda row: (-row["peer_count"], row["name"]))

    return {
        "task": (
            {
                "id": task.id,
                "task_no": task.task_no,
                "status": task.status,
                "finished_at": task.finished_at,
            }
            if task is not None
            else None
        ),
        # 总价来自任务快照：人工确认 > 自动识别，与报价清单页同口径
        "companies": [
            _company_payload(db, company, task) for company in companies
        ],
        "rows": rows,
        "summary": {
            "row_count": len(rows),
            "cell_count": cell_count,
            "flagged_cell_count": flagged_cells,
            "identical_cell_count": identical_cells,
            "company_count": len(companies),
        },
        "reason": (
            ""
            if rows
            else "未从正文识别到可比较的逐项单价。清单需要是「项目名称 + 单价」的行式表格；"
            "嵌套合并单元格或纯图片清单无法自动解析。"
        ),
    }


def _price_rules(task: AnalysisTask) -> dict:
    """矩阵判定阈值：沿用价格维度的规则版本。"""
    thresholds = (task.analysis_config or {}).get("thresholds") or {}
    from app.analysis.thresholds import price_thresholds

    rules = price_thresholds(thresholds)
    return {
        # 完全相同：两位小数内无差异（与逐项单价证据同一判定）
        "identical_tolerance": rules.get("price_line_item_identical_tolerance", 0.0001),
        # 偏离：相对同一分项的多家中位价超过该比例即提示
        "outlier_ratio": rules.get("price_matrix_outlier_ratio", 0.3),
    }


def _classify(
    amount: float,
    peers: list[float],
    *,
    identical_tolerance: float,
    outlier_ratio: float,
) -> str:
    """单个单元格的价格状态。

    判定顺序：相同 > 接近 > 偏离 > 同行 > 独有。相同优先，是因为「多家报出
    同一个价格」本身就是线索，即使这个价格也偏离中位价，也应该先说「一致」。
    """
    if not peers:
        return "single"
    diffs = [
        abs(amount - other) / max(abs(amount), abs(other), 1e-9) for other in peers
    ]
    if min(diffs) <= identical_tolerance:
        return "identical"
    if min(diffs) <= max(identical_tolerance, 0.001):
        return "near"
    # 偏离只有在「有至少两家其他企业可以互相印证」时才有意义：
    # 两家企业报价不同时，任何一方都会被算成偏离另一方，那不是异常而是常态，
    # 标出来只会淹没真正的线索。这里要求 peers 至少两家，并以其中位价为参照。
    if len(peers) >= 2:
        peer_median = median(peers)
        if peer_median > 0:
            deviation = abs(amount - peer_median) / peer_median
            if deviation >= outlier_ratio:
                return "outlier"
    return "peer"


def _company_payload(
    db: Session, company: Company, task: AnalysisTask | None
) -> dict:
    """企业的总价信息（与报价速览同口径：人工/导入 > 自动识别）。"""
    from app.services.quote_service import _active_quote

    entries = (
        ((task.analysis_config or {}).get("price_snapshot") or {}).get("entries") or {}
        if task is not None
        else {}
    )
    entry = entries.get(company.id) or {}
    manual = _active_quote(db, company.project_id, company.id)
    amount = float(manual.amount) if manual else entry.get("amount")
    return {
        "id": company.id,
        "name": company.company_name,
        "total_amount": round(float(amount), 2) if amount is not None else None,
        "source_kind": (
            manual.source_kind if manual else (entry.get("source_kind") or None)
        ),
        # 人工确认后不再提示「待确认」
        "needs_confirmation": bool(entry.get("needs_confirmation")) and not manual,
    }
