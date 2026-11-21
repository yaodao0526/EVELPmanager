"""EVE 忠诚点兑换规划器 - FastAPI 后端。

启动：python app.py  →  http://127.0.0.1:8000
ESI 授权：需先在项目目录创建 config.json（见 esi.py 文档注释）
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import esi
import enrich
import paths
import planner
import starweb

app = FastAPI(title="EVE LP Planner")

JITA_REGION_ID = 10000002  # 伏尔戈（The Forge，starweb 中文译名）

STATIC_DIR = paths.resource_path("static")
PLANS_PATH = paths.data_path("plans.json")
LP_COSTS_PATH = paths.data_path("lp_costs.json")
FLOATER_DATA = paths.data_path("floater_data.json")
FLOATER_PORT = 45678


class Entry(BaseModel):
    corporationId: int
    lp: float = 0  # 该公司的 LP 数量


class PlanRequest(BaseModel):
    # 市场固定 starweb 的 10000002（中文名：伏尔戈），regionId 仅为兼容旧前端保留
    regionId: int = 10000002
    entries: list[Entry] = Field(default_factory=list)
    isk: float = 0  # 钱包 ISK 预算
    days: int = 7  # 交易量统计天数
    tax: float = 4.5  # 销售税率 %
    scope: float = 0.05
    productPrice: str = "buy"  # 产品计价口径：buy 买单 / sell 卖单
    materialPrice: str = "sell"  # 材料计价口径
    marketShare: float = 0.05  # 每天最多消化市场当日销量的比例（0 不限）
    sellDays: int = 3  # 预计卖完的天数
    ownMaterials: bool = False  # 材料已自备，不计入 ISK 预算
    minProfitShare: float = 0.01  # 收益占比下限（0~1）
    minDailySales: float = 50_000_000.0  # 最低日销售额（ISK/天）
    excludeItemIds: list[int] = Field(default_factory=list)
    brokerFee: float = 2.5  # 经纪人费 %（Broker Relations 后一般 2.5~3）
    listing: bool = False  # True=挂卖单等成交（付经纪人费），False=直接砸买单
    maxLpShare: float = 0.25  # 单一商品最多占用多少比例的该公司 LP
    minItems: int = 5  # 至少换几种商品
    impact: float = 0.10  # 无采样曲线时，价格冲击系数（每吃 1 倍日成交量下跌比例）


@app.get("/api/meta")
def meta() -> dict:
    return {"regions": starweb.regions(), "factions": starweb.factions()}


@app.post("/api/plan")
def plan(req: PlanRequest) -> dict:
    # 公司名称映射
    corp_names: dict[int, str] = {}
    for f in starweb.factions():
        for c in f.get("Corporations", []):
            corp_names[int(c["CorporationId"])] = str(c["CorporationName"])

    all_offers: list[planner.Offer] = []
    errors: list[str] = []

    region_id = req.regionId or JITA_REGION_ID
    for e in req.entries:
        if e.lp <= 0:
            continue
        name = corp_names.get(e.corporationId, str(e.corporationId))
        try:
            data = starweb.offers(
                region_id,
                e.corporationId,
                days=req.days,
                tax=req.tax,
                scope=req.scope,
                product_price=req.productPrice,
                material_price=req.materialPrice,
            )
        except Exception as ex:  # noqa: BLE001
            errors.append(f"【{name}】报价拉取失败：{ex}")
            continue
        for d in data:
            all_offers.append(
                planner.Offer.from_api(d, e.corporationId, name, days=req.days)
            )

    lp_budgets = {e.corporationId: e.lp for e in req.entries if e.lp > 0}

    if not all_offers:
        return {"rows": [], "summary": None, "candidates": [], "errors": errors}

    # 价格曲线 / 均价 / 风险标记：全部来自 starweb（失败自动降级为线性衰减模型）
    try:
        source_info = enrich.enrich(
            all_offers,
            region_id=region_id,
            days=req.days,
            tax=req.tax,
            product_price=req.productPrice,
            material_price=req.materialPrice,
        )
    except Exception as ex:  # noqa: BLE001
        source_info = {"source": "eve.starweb.cc", "error": str(ex)}

    result = planner.build_plan(
        all_offers,
        lp_budgets,
        req.isk,
        market_share=req.marketShare,
        sell_days=req.sellDays,
        own_materials=req.ownMaterials,
        broker_fee=max(0.0, req.brokerFee) / 100.0,
        listing=req.listing,
        min_profit_share=min(1.0, max(0.0, req.minProfitShare)),
        min_daily_sales=max(0.0, req.minDailySales),
        exclude_item_ids=req.excludeItemIds,
        max_lp_share=min(1.0, max(0.0, req.maxLpShare)),
        min_items=max(1, req.minItems),
        impact=max(0.0, req.impact),
        lp_unit_costs={
            cid: float(_load_lp_costs().get(str(cid), 0) or 0) for cid in lp_budgets
        },
    )

    # 净利润 = 总利润 − LP 消耗 × 每 LP 获取成本（未设置的按 0）
    lp_costs = _load_lp_costs()
    for r in result["rows"]:
        unit = float(lp_costs.get(str(r["corporationId"]), 0) or 0)
        r["lpUnitCost"] = unit
        r["netProfit"] = round(r["profit"] - r["lpCost"] * unit, 2)
    for c in result["candidates"]:
        unit = float(lp_costs.get(str(c["corporationId"]), 0) or 0)
        c["netProfit"] = round(c["profit"] - c["lpCost"] * unit, 2)
    if result["summary"]:
        s = result["summary"]
        s["lpCostTotal"] = round(sum(r["lpCost"] * r["lpUnitCost"] for r in result["rows"]), 2)
        s["netProfit"] = round(s["totalProfit"] - s["lpCostTotal"], 2)

    result["errors"] = errors
    result["source"] = source_info
    result["regionId"] = region_id
    result["regionName"] = source_info.get("regionName") or str(region_id)
    return result


# ---------- ESI 授权与钱包 ----------

@app.get("/api/esi/status")
def esi_status() -> dict:
    return esi.status()


@app.get("/api/esi/login", response_model=None)
def esi_login():
    if not esi.is_configured():
        return {"error": "not_configured"}
    try:
        return RedirectResponse(esi.build_login_url())
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@app.get("/api/esi/callback")
def esi_callback(code: str = "", state: str = "") -> RedirectResponse:
    """EVE SSO 回调：换 token 后直达钱包页（带 tab=wallet）。"""
    if not code:
        return RedirectResponse("/?tab=wallet&esi_error=" + quote("缺少授权码"))
    try:
        esi.exchange_code(code, state)
    except requests.HTTPError as ex:
        detail = str(ex.response.text)[:200] if ex.response is not None else str(ex)
        return RedirectResponse("/?tab=wallet&esi_error=" + quote(f"令牌交换失败：{detail}"))
    except Exception as ex:  # noqa: BLE001
        return RedirectResponse("/?tab=wallet&esi_error=" + quote(str(ex)))
    return RedirectResponse("/?tab=wallet")


@app.post("/api/esi/logout")
def esi_logout() -> dict:
    esi.clear_token()
    return {"ok": True}


@app.get("/api/wallet")
def wallet() -> dict:
    try:
        return esi.wallet_summary()
    except RuntimeError as ex:
        raise HTTPException(status_code=401, detail=str(ex)) from ex
    except requests.HTTPError as ex:
        raise HTTPException(
            status_code=502,
            detail=f"ESI 请求失败：{ex.response.status_code if ex.response is not None else '?'}",
        ) from ex


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- LP 获取成本设置 ----------

def _load_lp_costs() -> dict:
    if LP_COSTS_PATH.exists():
        try:
            return json.loads(LP_COSTS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


class LpCostsRequest(BaseModel):
    costs: dict[str, float] = Field(default_factory=dict)


@app.get("/api/lp-costs")
def get_lp_costs() -> dict:
    return {"costs": _load_lp_costs()}


@app.post("/api/lp-costs")
def save_lp_costs(req: LpCostsRequest) -> dict:
    costs = {str(k): float(v) for k, v in req.costs.items() if float(v or 0) > 0}
    LP_COSTS_PATH.write_text(
        json.dumps(costs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True}


# ---------- 计划存档 ----------

def _load_plans() -> list[dict]:
    if PLANS_PATH.exists():
        try:
            return json.loads(PLANS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def _save_plans(plans: list[dict]) -> None:
    PLANS_PATH.write_text(
        json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class PlanRow(BaseModel):
    itemName: str
    copies: int = 0
    totalQuantity: int = 0
    lpCost: float = 0
    spend: float = 0
    profit: float = 0
    netProfit: float = 0


class SavePlanRequest(BaseModel):
    totalLp: float = 0
    totalSpend: float = 0
    totalIncome: float = 0
    totalProfit: float = 0
    netProfit: float = 0
    lpCostTotal: float = 0
    items: int = 0
    avgIsPerLp: float = 0
    rows: list[PlanRow] = Field(default_factory=list)


@app.post("/api/plans/save")
def save_plan(req: SavePlanRequest) -> dict:
    plans = _load_plans()
    plan = {
        "id": uuid.uuid4().hex[:12],
        "createdAt": datetime.now().astimezone().isoformat(timespec="minutes"),
        "totalLp": round(req.totalLp, 2),
        "totalSpend": round(req.totalSpend, 2),
        "totalIncome": round(req.totalIncome, 2),
        "totalProfit": round(req.totalProfit, 2),
        "netProfit": round(req.netProfit, 2),
        "lpCostTotal": round(req.lpCostTotal, 2),
        "items": req.items,
        "avgIsPerLp": round(req.avgIsPerLp, 2),
        "status": "active",
        "completedAt": None,
        "rows": [
            {
                "itemName": r.itemName,
                "copies": r.copies,
                "totalQuantity": r.totalQuantity,
                "lpCost": round(r.lpCost, 2),
                "spend": round(r.spend, 2),
                "profit": round(r.profit, 2),
                "netProfit": round(r.netProfit, 2),
            }
            for r in req.rows
        ],
    }
    plans.append(plan)
    _save_plans(plans)
    return {"plan": plan}


@app.get("/api/plans")
def list_plans() -> dict:
    plans = sorted(_load_plans(), key=lambda p: p.get("createdAt", ""), reverse=True)

    # 本月统计（按本地时区，只统计已完成的计划）
    now = datetime.now().astimezone()
    month_done = [
        p
        for p in plans
        if p.get("status") == "done" and p.get("completedAt")
        and datetime.fromisoformat(p["completedAt"]).astimezone().year == now.year
        and datetime.fromisoformat(p["completedAt"]).astimezone().month == now.month
    ]
    total_profit = round(
        sum(p.get("netProfit", p.get("totalProfit", 0)) for p in month_done), 2
    )
    stats = {
        "monthDone": len(month_done),
        "monthProfit": total_profit,
        "monthAvgProfit": round(total_profit / len(month_done), 2) if month_done else 0,
        "activeCount": sum(1 for p in plans if p.get("status") == "active"),
    }
    return {"plans": plans, "stats": stats}


@app.post("/api/plans/{plan_id}/complete")
def complete_plan(plan_id: str) -> dict:
    plans = _load_plans()
    for p in plans:
        if p.get("id") == plan_id and p.get("status") != "done":
            p["status"] = "done"
            p["completedAt"] = datetime.now().astimezone().isoformat(timespec="minutes")
            break
    _save_plans(plans)
    return {"ok": True}


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: str) -> dict:
    plans = [p for p in _load_plans() if p.get("id") != plan_id]
    _save_plans(plans)
    return {"ok": True}


def _floater_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", FLOATER_PORT), timeout=0.3):
            return True
    except OSError:
        return False


@app.post("/api/plans/{plan_id}/floater")
def open_floater(plan_id: str) -> dict:
    """在屏幕右下角打开置顶悬浮窗，展示该计划兑换清单（重复调用只刷新内容）。"""
    plan = next((p for p in _load_plans() if p.get("id") == plan_id), None)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在")
    payload = {
        "title": f"兑换清单 · {str(plan.get('createdAt', ''))[:16].replace('T', ' ')}",
        "rows": plan.get("rows", []),
        "summary": {
            "totalLp": plan.get("totalLp", 0),
            "totalSpend": plan.get("totalSpend", 0),
            "netProfit": plan.get("netProfit", plan.get("totalProfit", 0)),
        },
    }
    FLOATER_DATA.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not _floater_running():
        if paths.is_frozen():
            cmd = [sys.executable, "--floater"]
        else:
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            if not Path(exe).exists():
                exe = sys.executable
            cmd = [exe, str(paths.resource_path("floater.py"))]
        try:
            subprocess.Popen(
                cmd,
                cwd=str(paths.data_dir()),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"悬浮窗启动失败：{ex}") from ex
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import os

    import uvicorn

    # 生产（bat 启动）不开启 reload，避免 worker 重启导致 ESI 登录流程中断
    dev = os.environ.get("EVE_DEV") == "1"
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=dev)
