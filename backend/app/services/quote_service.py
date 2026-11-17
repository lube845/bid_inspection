"""投标报价的人工导入与补录.

价格维度的自动抽取（``analysis/price.py``）依赖正文里能匹配到固定写法；一旦格式
不符，整个维度会「识别不到报价 → 没有异常」，这是验收报告里最典型的静默失效。

本服务提供两种人工权威输入：

- **批量导入**：Excel/CSV 模板，按企业名称或统一社会信用代码匹配；
- **页面补录**：单家企业单条报价。

数据只落在 ``price_quote``（人工权威值），自动抽取结果不进库——它属于任务状态，
由 ``analysis_service._ensure_price_snapshot`` 固化到任务快照，历史任务才可复现。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.price import parse_upper_amount
from app.models import AnalysisTask, Company, PriceQuote, Project, User
from app.models.base import utcnow
from app.services import audit

# 模板列：企业名称与信用代码二选一即可
TEMPLATE_HEADERS = ["企业名称", "统一社会信用代码", "投标总报价（元）", "大写金额", "备注"]

_NUMBER_CLEAN = re.compile(r"[^0-9.\-]")
_UNIT_HINT = re.compile(r"万元|千元|亿元")

# 导入时金额超过该量级视为疑似单位写错（元 vs 万元），给 warning 而不是直接拒绝
_SUSPICIOUS_AMOUNT = 1_000_000_000


class QuoteError(Exception):
    """报价相关的业务校验错误。"""


def _clean_amount(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if _UNIT_HINT.search(text):
        # "998.6 万元" 这类写法交给行级校验提示，不在数值清洗里静默换算
        return None
    cleaned = _NUMBER_CLEAN.sub("", text)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _active_quote(db: Session, project_id: str, company_id: str) -> PriceQuote | None:
    return db.scalars(
        select(PriceQuote).where(
            PriceQuote.project_id == project_id,
            PriceQuote.company_id == company_id,
            PriceQuote.is_active.is_(True),
        )
    ).first()


def _auto_entries(db: Session, project_id: str) -> dict[str, dict]:
    """最新分析任务里自动识别到的报价（只读参考，不是人工权威值）。"""
    from app.services import task_scope

    task_id = task_scope.latest_task_id(db, project_id)
    if not task_id:
        return {}
    task = db.get(AnalysisTask, task_id)
    if task is None:
        return {}
    entries = ((task.analysis_config or {}).get("price_snapshot") or {}).get("entries") or {}
    return {
        company_id: entry
        for company_id, entry in entries.items()
        if (entry.get("source_kind") or "auto") == "auto"
    }


def list_quotes(db: Session, project: Project) -> list[dict]:
    companies = list(
        db.scalars(
            select(Company).where(
                Company.project_id == project.id, Company.deleted_at.is_(None)
            ).order_by(Company.sort_order)
        )
    )
    quotes = {
        quote.company_id: quote
        for quote in db.scalars(
            select(PriceQuote).where(
                PriceQuote.project_id == project.id, PriceQuote.is_active.is_(True)
            )
        )
    }
    # 自动识别结果只存在于任务快照里。清单页把它们以只读形式一并展示：
    # 否则「分析已识别 4/4、清单页全部未录入」，人工无法复核自动金额。
    auto = _auto_entries(db, project.id)
    items = []
    for company in companies:
        manual = quotes.get(company.id)
        detected = auto.get(company.id)
        items.append(
            {
                "company_id": company.id,
                "company_name": company.company_name,
                "credit_code": company.credit_code,
                "quote": quote_payload(manual) if manual else None,
                "detected": (
                    {
                        "amount": float(detected.get("amount") or 0),
                        "amount_upper": detected.get("amount_upper"),
                        "raw": detected.get("raw") or "",
                        "source": detected.get("source") or {},
                        "needs_confirmation": bool(detected.get("needs_confirmation")),
                        "alternative_amounts": detected.get("alternative_amounts") or [],
                    }
                    if detected
                    else None
                ),
                # 人工报价优先；没有人工值时才以自动识别值为准
                "effective_source": (
                    "manual"
                    if manual
                    else ("auto" if detected else None)
                ),
            }
        )
    return items


def quote_payload(quote: PriceQuote) -> dict:
    return {
        "id": quote.id,
        "amount": float(quote.amount),
        "amount_upper": quote.amount_upper,
        "source_kind": quote.source_kind,
        "source_ref": quote.source_ref or {},
        "version_no": quote.version_no,
        "updated_at": quote.updated_at,
        "raw_text": quote.raw_text,
    }


def set_quote(
    db: Session,
    project: Project,
    company: Company,
    amount: float,
    *,
    amount_upper: str | None = None,
    source_kind: str = "manual",
    source_ref: dict | None = None,
    raw_text: str = "",
    actor: User | None = None,
    commit: bool = True,
) -> PriceQuote:
    """写入一条新的有效报价；旧版本保留为历史。"""
    if amount is None or amount <= 0:
        raise QuoteError("投标总报价必须大于 0")
    current = _active_quote(db, project.id, company.id)
    version = (current.version_no + 1) if current else 1
    if current:
        current.is_active = False
        current.superseded_at = utcnow()
    quote = PriceQuote(
        project_id=project.id,
        company_id=company.id,
        amount=round(float(amount), 2),
        amount_upper=(amount_upper or "").strip() or None,
        source_kind=source_kind,
        source_ref=source_ref or {},
        raw_text=raw_text or "",
        version_no=version,
        is_active=True,
        created_by=actor.id if actor else None,
    )
    db.add(quote)
    db.flush()
    audit.log_audit(
        db,
        "price_quote.manual_entry" if source_kind == "manual" else "price_quote.import",
        "price_quote",
        quote.id,
        actor,
        {
            "company_name": company.company_name,
            "amount": float(quote.amount),
            "version_no": version,
        },
        entity_name=company.company_name,
    )
    if commit:
        db.commit()
        db.refresh(quote)
    return quote


def delete_quote(
    db: Session, project: Project, company: Company, actor: User | None = None
) -> None:
    current = _active_quote(db, project.id, company.id)
    if current is None:
        raise QuoteError("该企业当前没有可用报价")
    current.is_active = False
    current.superseded_at = utcnow()
    audit.log_audit(
        db,
        "price_quote.delete",
        "price_quote",
        current.id,
        actor,
        {"company_name": company.company_name, "version_no": current.version_no},
        entity_name=company.company_name,
    )
    db.commit()


# ---------------------------------------------------------------- 导入


def build_template() -> bytes:
    """生成报价导入模板（xlsx）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "报价单"
    ws.append(TEMPLATE_HEADERS)
    ws.append(["示例科技有限公司", "91110000XXXXXXXXXX", 10000000, "壹仟万元整", "示例行，可删除"])
    for index, width in enumerate([32, 24, 20, 28, 24], start=1):
        ws.column_dimensions[chr(64 + index)].width = width
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _read_rows(filename: str, data: bytes) -> list[list]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix in {"xlsx", "xlsm"}:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.active
        return [list(row) for row in ws.iter_rows(values_only=True)]
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
    raise QuoteError("仅支持 .xlsx / .csv 格式的报价单")


def import_quotes(
    db: Session,
    project: Project,
    filename: str,
    data: bytes,
    actor: User | None = None,
    *,
    dry_run: bool = True,
) -> dict:
    """解析报价单。

    ``dry_run=True`` 只返回预览与逐行校验结果；前端确认后以 ``dry_run=False``
    提交。这样可以避免一次错表把整批报价写脏。
    """
    rows = _read_rows(filename, data)
    if not rows:
        raise QuoteError("报价单内容为空")
    header = [str(cell or "").strip() for cell in rows[0]]
    if "企业名称" not in header and "统一社会信用代码" not in header:
        raise QuoteError("表头缺少「企业名称」或「统一社会信用代码」列")
    columns = {name: index for index, name in enumerate(header) if name}

    companies = list(
        db.scalars(
            select(Company).where(
                Company.project_id == project.id, Company.deleted_at.is_(None)
            )
        )
    )
    by_name = {company.company_name.strip(): company for company in companies}
    by_code = {
        company.credit_code.strip(): company
        for company in companies
        if company.credit_code and company.credit_code.strip()
    }

    seen: set[str] = set()
    parsed: list[dict] = []
    errors: list[dict] = []
    warnings: list[dict] = []
    batch_id = datetime.now().strftime("%Y%m%d%H%M%S")

    for offset, row in enumerate(rows[1:], start=2):
        if not any(str(cell or "").strip() for cell in row):
            continue

        def cell(name: str) -> str:
            index = columns.get(name)
            if index is None or index >= len(row):
                return ""
            return str(row[index] or "").strip()

        name = cell("企业名称")
        code = cell("统一社会信用代码")
        raw_amount = cell("投标总报价（元）")
        amount_upper = cell("大写金额")
        remark = cell("备注")

        company = by_code.get(code) if code else None
        if company is None and name:
            company = by_name.get(name)
        if company is None:
            errors.append(
                {"row": offset, "field": "company", "message": f"未找到企业：{name or code or '（空）'}"}
            )
            continue
        if company.id in seen:
            errors.append(
                {"row": offset, "field": "company", "message": f"企业「{company.company_name}」在文件中重复出现"}
            )
            continue

        amount = _clean_amount(raw_amount)
        if amount is None:
            errors.append(
                {"row": offset, "field": "amount", "message": "投标总报价无法解析，请填写纯数字金额（单位：元）"}
            )
            continue
        if amount <= 0:
            errors.append({"row": offset, "field": "amount", "message": "投标总报价必须大于 0"})
            continue
        if amount >= _SUSPICIOUS_AMOUNT:
            warnings.append(
                {
                    "row": offset,
                    "field": "amount",
                    "message": f"金额 {amount:,.2f} 元偏大，请确认是否误填为万元",
                }
            )
        if amount_upper:
            parsed_upper = parse_upper_amount(amount_upper)
            if parsed_upper is None:
                warnings.append(
                    {"row": offset, "field": "amount_upper", "message": "大写金额无法解析，已按数字金额处理"}
                )
            elif abs(parsed_upper - amount) > 0.01:
                errors.append(
                    {
                        "row": offset,
                        "field": "amount_upper",
                        "message": f"大小写金额不一致：数字 {amount:,.2f} 元，大写解析为 {parsed_upper:,.2f} 元",
                    }
                )
                continue

        seen.add(company.id)
        parsed.append(
            {
                "row": offset,
                "company_id": company.id,
                "company_name": company.company_name,
                "amount": amount,
                "amount_upper": amount_upper or None,
                "raw_text": remark,
            }
        )

    matched_ids = {item["company_id"] for item in parsed}
    unmatched_companies = [
        company.company_name
        for company in companies
        if company.participate_analysis and company.id not in matched_ids
    ]
    if unmatched_companies:
        warnings.append(
            {
                "row": 0,
                "field": "company",
                "message": "以下企业未出现在报价单中：" + "、".join(unmatched_companies[:10]),
            }
        )

    result = {
        "batch_id": batch_id,
        "dry_run": dry_run,
        "rows": parsed,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "total": len(parsed),
            "error_count": len(errors),
            "warning_count": len(warnings),
            "matched_companies": len(matched_ids),
        },
    }
    if dry_run or errors:
        return result

    for item in parsed:
        company = db.get(Company, item["company_id"])
        set_quote(
            db,
            project,
            company,
            item["amount"],
            amount_upper=item["amount_upper"],
            source_kind="import",
            source_ref={"batch_id": batch_id, "row": item["row"], "file": filename},
            raw_text=item["raw_text"],
            actor=actor,
            commit=False,
        )
    db.commit()
    return result
