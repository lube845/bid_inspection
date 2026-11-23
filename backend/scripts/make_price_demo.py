"""生成一个「价格比对」演示项目（含 4 家企业与价格 Excel 数据）。

用法（在 api 容器内执行）::

    docker compose exec api python scripts/make_price_demo.py

脚本会创建（或复用）项目「价格比对演示项目」，为 4 家企业写入价格资料，
数据刻意编排成四种典型情形，便于肉眼核对矩阵着色：

- 甲、乙：多数分项单价**完全一致**（红）——最值得复核的线索；
- 丙：多数分项在合理区间内小幅浮动（白），个别分项明显**偏高**（紫）；
- 丁：只报了部分分项（灰），且与甲、乙有一项相同。

重复执行不会产生重复数据：同名项目会被清空重建。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models import Company, Project  # noqa: E402
from app.services import project_service, quote_service  # noqa: E402

DEMO_CODE = "PRICE-MATRIX-DEMO"

# (清单项, 单位, 数量, 中标参考单价)
ITEMS = [
    ("土方开挖（含运输）", "m3", 1200, 48.00),
    ("回填土方", "m3", 800, 32.00),
    ("C30 混凝土浇筑", "m3", 450, 486.00),
    ("钢筋制安", "t", 120, 4320.00),
    ("模板工程", "m2", 2600, 62.00),
    ("外墙保温", "m2", 1800, 118.00),
    ("屋面防水", "m2", 950, 86.00),
    ("给排水安装", "项", 1, 268000.00),
    ("电气安装", "项", 1, 316000.00),
    ("措施费", "项", 1, 158000.00),
]

# 每家企业的价格系数 + 需要「照抄」的分项（演示雷同）
COMPANIES = [
    ("甲建设工程有限公司", 1.000, None),
    ("乙建筑安装有限公司", 1.000, None),
    ("丙工程技术有限公司", 1.085, {"钢筋制安": 1.55}),  # 个别分项明显偏离
    ("丁市政工程有限公司", 1.030, None),
]
# 丁只报部分分项
DING_SUBSET = {"土方开挖（含运输）", "C30 混凝土浇筑", "模板工程", "措施费"}


def _money(value: float) -> float:
    return round(value, 2)


def build_items(company_name: str, factor: float, overrides: dict | None) -> list[dict]:
    overrides = overrides or {}
    subset = DING_SUBSET if company_name.startswith("丁") else None
    rows = []
    for name, unit, qty, price in ITEMS:
        if subset is not None and name not in subset:
            continue
        # 甲、乙完全一致：用同一份价格；丙按系数浮点；丁部分照抄 + 部分上浮
        if company_name.startswith("甲") or company_name.startswith("乙"):
            unit_price = price
        else:
            unit_price = price * factor
        unit_price *= overrides.get(name, 1.0)
        rows.append(
            {
                "name": name,
                "key": name,
                "unit": unit,
                "quantity": qty,
                "unit_price": _money(unit_price),
            }
        )
    return rows


def main() -> None:
    db = SessionLocal()
    try:
        existing = db.scalars(
            select(Project).where(Project.project_code == DEMO_CODE)
        ).first()
        if existing is not None:
            print(f"移除旧演示项目 {existing.project_name}")
            project_service.purge_project(db, existing)
            db.commit()

        project = Project(
            project_name="价格比对演示项目",
            project_code=DEMO_CODE,
            description=(
                "用于演示价格比对页：甲乙单价完全一致、丙个别分项明显偏高、丁只报部分分项。"
                "直接打开「价格比对」页签即可看到颜色标记与悬浮明细。"
            ),
        )
        db.add(project)
        db.commit()

        for index, (name, factor, overrides) in enumerate(COMPANIES):
            company = Company(
                project_id=project.id,
                company_name=name,
                sort_order=index,
                participate_analysis=True,
            )
            db.add(company)
            db.flush()

            items = build_items(name, factor, overrides)
            total = _money(sum(item["unit_price"] * item["quantity"] for item in items))
            quote_service.set_quote(
                db,
                project,
                company,
                total,
                source_kind="import",
                source_ref={"filename": f"{name}-价格清单.xlsx", "demo": True},
                raw_text="演示数据",
                commit=False,
            )
            quote_service.set_price_items(
                db,
                project,
                company,
                items,
                source_kind="import",
                source_ref={"filename": f"{name}-价格清单.xlsx", "demo": True},
                commit=False,
            )
            print(f"  {name}: {len(items)} 个分项，总价 {total:,.2f} 元")

        db.commit()
        print(f"\n完成：项目「{project.project_name}」({project.id})")
        print(f"打开：http://localhost:8888/#/projects/{project.id}?tab=price-matrix")
    finally:
        db.close()


if __name__ == "__main__":
    main()
