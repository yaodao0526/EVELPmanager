"""把 starweb 报价补全成可直接求解的对象。

数据来源**只有** https://eve.starweb.cc/lpstore（见 ``starweb.py``）：
- 价格曲线：对同一公司用多个 ``scope`` 采样，得到「卖掉多少件 → 均价多少」
- 均价基准：``TxnAmount / Volume``（近 N 日成交量加权均价）
- 出货速度：``SaleIndex``

这里不做任何来自其它数据源的推测，只把 starweb 已有字段组织成 planner 需要的形状。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import starweb

# 风险标记：code -> (level, 中文标签)
FLAG_META = {
    "price_spike": ("warn", "高于近期均价"),
    "price_dip": ("info", "低于近期均价"),
    "slow_sale": ("warn", "出货慢"),
    "tiny_market": ("warn", "日成交不足一份"),
    "no_curve": ("info", "无价格曲线"),
}

PRICE_SPIKE_RATIO = 1.30   # 现价 ≥ 近 N 日均价 × 1.30 → 价位虚高
PRICE_DIP_RATIO = 0.70     # 现价 ≤ 近 N 日均价 × 0.70 → 价位偏低
SLOW_SALE_INDEX = 3        # 出货指数（starweb SaleIndex）低于该值视为难卖（约最低 12% 分位）


def _flags(offer) -> list[dict]:
    """只用 starweb 字段判断的市场风险。"""
    out: list[dict] = []
    if offer.avg_price > 0 and offer.price > 0:
        ratio = offer.price / offer.avg_price
        if ratio >= PRICE_SPIKE_RATIO:
            out.append({
                "code": "price_spike",
                "level": FLAG_META["price_spike"][0],
                "label": FLAG_META["price_spike"][1],
                "detail": f"当前 {offer.price:,.0f} / 近 {offer.days} 日均价 {offer.avg_price:,.0f}（{ratio:.0%}）",
            })
        elif ratio <= PRICE_DIP_RATIO:
            out.append({
                "code": "price_dip",
                "level": FLAG_META["price_dip"][0],
                "label": FLAG_META["price_dip"][1],
                "detail": f"当前 {offer.price:,.0f} / 近 {offer.days} 日均价 {offer.avg_price:,.0f}（{ratio:.0%}）",
            })
    if offer.sale_index and offer.sale_index < SLOW_SALE_INDEX:
        out.append({
            "code": "slow_sale",
            "level": FLAG_META["slow_sale"][0],
            "label": FLAG_META["slow_sale"][1],
            "detail": f"出货指数 {offer.sale_index}",
        })
    if offer.volume and offer.quantity and offer.volume < offer.quantity:
        out.append({
            "code": "tiny_market",
            "level": FLAG_META["tiny_market"][0],
            "label": FLAG_META["tiny_market"][1],
            "detail": f"日成交 {offer.volume:,.0f} 件 < 每份 {offer.quantity:,.0f} 件",
        })
    if offer.curve_points is not None and len(offer.curve_points) < 2:
        out.append({
            "code": "no_curve",
            "level": FLAG_META["no_curve"][0],
            "label": FLAG_META["no_curve"][1],
            "detail": "只能用线性衰减估算价格冲击",
        })
    return out


def enrich(
    offers: list,
    *,
    region_id: int,
    days: int = 7,
    tax: float = 0.0,
    product_price: str = "buy",
    material_price: str = "sell",
    scopes: tuple[float, ...] = starweb.DEFAULT_SCOPES,
) -> dict:
    """给每个 Offer 补 ``curve_points`` / ``avg_price`` / ``flags``，并返回数据来源信息。"""
    by_corp: dict[int, list] = {}
    for o in offers:
        by_corp.setdefault(o.corporation_id, []).append(o)

    errors: list[str] = []
    with_curve = 0

    def fetch(corp_id: int):
        try:
            return corp_id, starweb.curves(
                region_id,
                corp_id,
                days=days,
                tax=tax,
                product_price=product_price,
                material_price=material_price,
                scopes=scopes,
            )
        except Exception as ex:  # noqa: BLE001
            return corp_id, {"__error__": str(ex)}

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(by_corp)))) as pool:
        fetched = dict(pool.map(fetch, list(by_corp)))

    for corp_id, group in by_corp.items():
        curves = fetched.get(corp_id) or {}
        if "__error__" in curves:
            errors.append(f"【{group[0].corporation_name}】价格曲线拉取失败：{curves['__error__']}")
            curves = {}

        for o in group:
            pts = curves.get(o.item_id) or []
            o.curve_points = [(float(q), float(p)) for q, p in pts] if pts else []
            o.avg_price = (
                float(o.txn_amount) / float(o.volume)
                if o.volume and o.txn_amount
                else 0.0
            )
            o.flags = _flags(o)
            if len(o.curve_points) >= 2:
                with_curve += 1

    return {
        "source": "eve.starweb.cc",
        "sourceUrl": "https://eve.starweb.cc/lpstore",
        "regionId": region_id,
        "regionName": starweb.region_name(region_id, default=str(region_id)),
        "offers": len(offers),
        "curves": with_curve,
        "scopes": list(scopes),
        "days": days,
        "errors": errors,
        "fetchedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
