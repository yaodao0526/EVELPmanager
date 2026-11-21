"""eve.starweb.cc LP 商店数据客户端。

本项目的**唯一**数据来源是第三方站点 https://eve.starweb.cc/lpstore 的公开 JSON 接口：
- /api/common/region     星域列表（中文名，如 10000002 = 伏尔戈）
- /api/lp/faction        势力 → 下属公司列表（中文名）
- /api/lp/offer          指定公司的 LP 商店报价（价格、利润、交易量、出货指数）

价格冲击曲线也由该接口自身给出：``scope`` 参数表示"卖出当日成交量的多少比例"，
``Price`` 返回的就是吃掉这么多量之后的**成交量加权均价**。因此对同一公司取多个
``scope`` 采样，就能还原出「卖得越多、均价越低」的阶梯曲线，无需再引入别的数据源。

内置进程内缓存，避免频繁请求给目标站点造成压力。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

BASE_URL = "https://eve.starweb.cc"
DEFAULT_HEADERS = {
    "User-Agent": "EveLpPlanner/0.3 (personal trading tool; contact: local)",
}

# 采样点：卖出当日成交量的 0.2% / 1% / 5% / 20%
DEFAULT_SCOPES = (0.002, 0.01, 0.05, 0.2)

# url+query -> (timestamp, data)
_cache: dict[str, tuple[float, Any]] = {}


def _get(url: str, params: dict[str, Any] | None = None, ttl: int = 600) -> Any:
    """GET JSON 接口，带 TTL 缓存。"""
    params = params or {}
    key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]

    resp = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    _cache[key] = (now, data)
    return data


def regions(lang: str = "zh") -> list[dict]:
    """星域列表：[{RegionId, RegionName}]"""
    return _get(f"{BASE_URL}/api/common/region", {"lang": lang}, ttl=3600)


def factions(lang: str = "zh") -> list[dict]:
    """势力列表：[{FactionId, FactionName, Corporations:[{CorporationId, CorporationName}]}]"""
    return _get(f"{BASE_URL}/api/lp/faction", {"lang": lang}, ttl=3600)


def offers(
    region_id: int,
    corporation_id: int,
    *,
    days: int = 7,
    tax: float = 0,
    scope: float = 0.05,
    product_price: str = "buy",
    material_price: str = "sell",
    lang: str = "zh",
) -> list[dict]:
    """指定公司 LP 商店报价。

    每个条目字段：
    OfferId, ItemId, ItemName, Quantity(每份数量), IskCost, LpCost,
    Materials[{ItemId, MaterialName, Quantity, Price, Cost}], MaterialCost,
    Price(产品单价), Income(预计收入), Profit(预计利润), Volume(交易量),
    TxnAmount(交易额), UnitProfit(每 LP 利润, 即 ISK/LP), SaleIndex(出货指数),
    Error, ErrorMessage
    """
    return _get(
        f"{BASE_URL}/api/lp/offer",
        {
            "regionId": region_id,
            "scope": scope,
            "corporationId": corporation_id,
            "lang": lang,
            "days": days,
            "tax": tax,
            "productPrice": product_price,
            "materialPrice": material_price,
        },
        ttl=600,
    )


def region_name(region_id: int, lang: str = "zh", default: str = "") -> str:
    """按 starweb 的中文译名返回星域名（查不到时返回 default）。"""
    try:
        for r in regions(lang):
            if int(r.get("RegionId", 0)) == int(region_id):
                return str(r.get("RegionName") or default)
    except Exception:  # noqa: BLE001
        pass
    return default


def curves(
    region_id: int,
    corporation_id: int,
    *,
    days: int = 7,
    tax: float = 0,
    product_price: str = "buy",
    material_price: str = "sell",
    scopes: tuple[float, ...] = DEFAULT_SCOPES,
    lang: str = "zh",
) -> dict[int, list[tuple[float, float]]]:
    """用多个 scope 采样还原每个物品的「卖出量 → 均价」曲线。

    返回 ``{item_id: [(累计件数, 均价), ...]}``，按件数升序。
    每个采样点代表：卖掉 ``scope × 日成交量`` 件时，成交均价为 ``Price``。
    """
    out: dict[int, dict[float, float]] = {}
    jobs = list(scopes)

    def fetch(scope: float):
        try:
            return scope, offers(
                region_id,
                corporation_id,
                days=days,
                tax=tax,
                scope=scope,
                product_price=product_price,
                material_price=material_price,
                lang=lang,
            )
        except Exception as ex:  # noqa: BLE001
            raise RuntimeError(f"scope={scope} 采样失败：{ex}") from ex

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(jobs)))) as pool:
        results = list(pool.map(fetch, jobs))

    for _scope, rows in results:
        for d in rows:
            item_id = int(d.get("ItemId", 0) or 0)
            volume = float(d.get("Volume", 0) or 0)
            price = float(d.get("Price", 0) or 0)
            if item_id <= 0 or volume <= 0 or price <= 0:
                continue
            out.setdefault(item_id, {})[round(_scope * volume, 2)] = price
    return {k: sorted(v.items()) for k, v in out.items()}
