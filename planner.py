"""LP 兑换规划算法（v2）。

问题定义（每家公司拥有独立的 LP 池，钱包 ISK 全局共享）：

    maximize  Σ [ 收入_i(n_i) − 成本_i · n_i ] · (1 − 经纪人费率)
    subject to:
        Σ lp_i · n_i ≤ LP_c              (对每家公司 c 独立)
        Σ spend_i · n_i ≤ ISK 预算        (spend_i = 商店 ISK + 材料成本)
        n_i ≤ min(流动性上限, 单商品 LP 占比上限)
        n_i ∈ ℤ≥0

与 v1 的关键差别：

1. **收入不再是线性的**。每份兑换的成交均价由 starweb 的多 scope 采样曲线决定（`PriceCurve`），
   吃穿浅档后单价逐步下跌，边际利润递减 —— 这使问题成为"可分离凹资源分配"，
   按"下一份的边际利润 / LP"贪心即为精确最优（单约束情形）。
2. **ISK 是显式约束，并有影子价格 λ**。对 ISK 约束做拉格朗日松弛后，
   每份成本按 (1+λ) 计价，二分 λ 使总支出贴合预算；λ 的量纲是
   "每 1 ISK 机会成本可换回的利润"，可直接读成"再多 1 亿本金能多赚多少"。
3. **均衡度改为硬约束**（单商品 LP 占比上限 + 最少品种数提示），
   不再对无量纲评分做指数衰减。
4. **账目按 offer_id 记账**，市场容量按 item_id 共享：
   同势力不同公司卖同一物品时不会互相串账。
"""
from __future__ import annotations

import heapq
import itertools
import math
from bisect import bisect_right
from dataclasses import dataclass, field

# 卖出曲线：没有采样曲线时，卖出量达到 1 倍日成交量，价格下跌约 10%
DEFAULT_IMPACT = 0.10
# 价格下跌的地板（再高估就会离谱了）
DEFAULT_FLOOR = 0.45


@dataclass
class Offer:
    offer_id: int
    item_id: int
    item_name: str
    quantity: int          # 每份数量
    isk_cost: float        # 商店收取的 ISK
    lp_cost: int           # 每份 LP 成本
    material_cost: float   # 材料市场成本
    materials: list        # 材料明细 [{name, quantity, isBlueprint}]
    price: float           # 产品单价（starweb 深度加权口径）
    income: float          # 预计收入（已扣销售税）
    profit: float          # 预计利润（已扣销售税与材料）
    volume: int            # 日均成交量（starweb 已按近 N 天取平均）
    velocity: float        # 销售速度：日均成交量
    txn_amount: float      # 日均交易额
    unit_profit: float     # 每 LP 利润（starweb 口径）
    sale_index: int        # 出货指数
    corporation_id: int
    corporation_name: str
    # ---- v2 扩展（由 app / enrich 填充，缺省时退化为线性模型）----
    tax_factor: float = 0.0                      # 收入保留比例 = 1 − 税率
    curve_points: list[tuple[float, float]] | None = None  # starweb 多 scope 采样 [(累计件数, 均价)]
    avg_price: float = 0.0                       # 近 N 日成交均价（TxnAmount / Volume）
    days: int = 7                                # starweb 统计窗口（天）
    flags: list = field(default_factory=list)    # 市场陷阱标记 [{code, level, label}]

    def __post_init__(self) -> None:
        if self.tax_factor <= 0:
            self.tax_factor = self._infer_tax_factor()

    def _infer_tax_factor(self) -> float:
        """从 starweb 的 Income / (数量 × 单价) 反推税后比例。"""
        denom = self.quantity * self.price
        if denom > 0 and self.income > 0:
            f = self.income / denom
            if 0.5 <= f <= 1.0:
                return f
        return 1.0

    @property
    def spend(self) -> float:
        """每份 ISK 现金支出（商店 ISK + 材料成本）。"""
        return self.isk_cost + self.material_cost

    @property
    def unit_item_profit(self) -> float:
        """单件商品利润（按 starweb 口径）。"""
        return self.profit / self.quantity if self.quantity > 0 else 0.0

    @classmethod
    def from_api(cls, d: dict, corp_id: int, corp_name: str, days: int = 7) -> "Offer":
        volume = int(d.get("Volume", 0) or 0)
        materials = [
            {
                "name": str(m.get("MaterialName", "")),
                "quantity": int(m.get("Quantity", 0) or 0),
                "isBlueprint": bool(m.get("IsBluePrint", False)),
            }
            for m in (d.get("Materials") or [])
        ]
        return cls(
            offer_id=int(d.get("OfferId", 0)),
            item_id=int(d.get("ItemId", 0)),
            item_name=str(d.get("ItemName", "")),
            quantity=int(d.get("Quantity", 1) or 1),
            isk_cost=float(d.get("IskCost", 0) or 0),
            lp_cost=int(d.get("LpCost", 0) or 0),
            material_cost=float(d.get("MaterialCost", 0) or 0),
            materials=materials,
            price=float(d.get("Price", 0) or 0),
            income=float(d.get("Income", 0) or 0),
            profit=float(d.get("Profit", 0) or 0),
            volume=volume,
            velocity=float(volume),
            txn_amount=float(d.get("TxnAmount", 0) or 0),
            unit_profit=float(d.get("UnitProfit", 0) or 0),
            sale_index=int(d.get("SaleIndex", 0) or 0),
            corporation_id=corp_id,
            corporation_name=corp_name,
            days=days,
        )


class PriceCurve:
    """卖出曲线：累计卖出 q 件时的毛收入（未扣税）。

    曲线点来自 starweb 的多 ``scope`` 采样：``[(累计件数, 均价)]``。
    有采样时按点连成分段线性的收入曲线；超过最后一个采样点后，边际价格沿最后
    一段的斜率继续下探，触及地板价（``floor × 首点均价``）后走平。
    没有采样时退化为「1 倍日成交量内按 ``impact`` 线性下跌」的模型。
    """

    def __init__(self, offer: Offer, impact: float = DEFAULT_IMPACT, floor: float = DEFAULT_FLOOR):
        self.base = offer.price if offer.price > 0 else 0.0
        self.velocity = max(float(offer.velocity), 1.0)
        self.impact = impact
        self.floor = floor
        self._q: list[float] = [0.0]
        self._r: list[float] = [0.0]

        pts: list[tuple[float, float]] = []
        for q, p in sorted(offer.curve_points or []):
            q, p = float(q), float(p)
            if q <= 0 or p <= 0:
                continue
            if pts and q <= pts[-1][0] * 1.0000001:  # 合并重复/乱序采样点
                pts[-1] = (pts[-1][0], max(pts[-1][1], p))
                continue
            pts.append((q, p))

        for q, p in pts:
            self._q.append(q)
            self._r.append(q * p)

        self.has_curve = len(pts) >= 2
        if self.has_curve:
            if len(pts) >= 3:
                marginal = (self._r[-1] - self._r[-2]) / max(self._q[-1] - self._q[-2], 1e-9)
            else:
                marginal = self._r[-1] / max(self._q[-1], 1e-9)
            avg_last = self._r[-1] / max(self._q[-1], 1e-9)
            self.tail_price = max(0.0, min(marginal, avg_last))
        else:
            # 单点采样时也能当"深度 = 该点件数"的曲线用
            self.tail_price = self._r[-1] / self._q[-1] if self._r[-1] > 0 else self.base
        self.depth = self._q[-1]
        self.head_price = self._r[1] / self._q[1] if self._r[1] > 0 else self.base

    # ---------- 收入 ----------
    def revenue(self, q: float) -> float:
        if q <= 0 or not self._q:
            return 0.0
        if q >= self.depth:
            return self._r[-1] + self._tail_integral(q - self.depth)
        i = bisect_right(self._q, q) - 1
        q0, r0 = self._q[i], self._r[i]
        q1, r1 = self._q[i + 1], self._r[i + 1]
        if q1 <= q0:
            return r1
        price = (r1 - r0) / (q1 - q0)
        return r0 + price * (q - q0)

    def _tail_integral(self, extra: float) -> float:
        p0 = self.tail_price
        if p0 <= 0 or extra <= 0:
            return 0.0
        v, a, fl = self.velocity, self.impact, self.floor
        if a <= 0:
            return p0 * extra
        j_cap = v * (1.0 - fl) / a          # 跌到地板价所需的件数
        j1 = min(extra, j_cap)
        total = p0 * (j1 - a * j1 * j1 / (2.0 * v))
        if extra > j_cap:
            total += p0 * fl * (extra - j_cap)
        return total

    def margin(self, q_from: float, q_to: float) -> float:
        return self.revenue(q_to) - self.revenue(q_from)


@dataclass
class _Ctx:
    """一次求解所需的静态上下文。"""
    offers: list[Offer]
    curves: list[PriceCurve]
    spend: list[float]
    caps: list[int]
    tax: list[float]
    lp_budgets: dict[int, float]
    lp_cost: list[float]  # 每 1 LP 的获取成本（ISK），用于净利口径排序
    # 排序时实际扣除的每 LP 成本。默认等于 lp_cost；勾选「LP 视为已有」时全为 0，
    # 这样 800 万 LP 会尽量用光，而不是被「每 LP 赚不过成本」提前筛掉。
    lp_cost_rank: list[float] = None  # type: ignore[assignment]
    # 非 None 时改用「偏好分」排序（0~1，越大越先拿）：
    #   LP 优先 = None（按每 LP 净利润边际）
    #   速度优先 / 均衡 = 预计算的分数（速度优先看每 LP 日成交额）
    sort_score: list[float] | None = None


def _lp_share_cap(offer: Offer, budget: float, max_share: float) -> int:
    if max_share <= 0 or budget <= 0 or offer.lp_cost <= 0:
        return 10 ** 9
    return max(0, int(max_share * budget // offer.lp_cost))


def _solve(
    ctx: _Ctx,
    *,
    isk_budget: float | None,
    broker_fee: float,
    listing: bool,
    lam: float,
) -> tuple[list[int], dict]:
    """按"下一份的边际利润 ÷ LP"贪心分配。

    ``isk_budget=None`` 表示不考虑 ISK 上限（用于二分 λ 时测量支出曲线）。
    ``lam`` 是 ISK 的影子价格（每 1 ISK 机会成本）。
    """
    lp_left = {c: float(v) for c, v in ctx.lp_budgets.items()}
    isk_left = float("inf") if isk_budget is None else float(isk_budget)
    copies = [0] * len(ctx.offers)
    fee_keep = (1.0 - broker_fee) if listing else 1.0

    heap: list[tuple[float, int, int]] = []
    seq = itertools.count()

    def marginal(i: int, n: int) -> float:
        o = ctx.offers[i]
        gross = ctx.curves[i].margin((n - 1) * o.quantity, n * o.quantity)
        income = gross * ctx.tax[i]
        return income * fee_keep - (o.isk_cost + o.material_cost)

    def push(i: int, n: int) -> None:
        o = ctx.offers[i]
        m = marginal(i, n)
        if ctx.sort_score is not None:
            # 偏好分模式（速度优先 / 均衡）：只挑"边际还赚钱"的，按预先算好的偏好排序
            if m > 0:
                heapq.heappush(heap, (-ctx.sort_score[i], next(seq), i))
            return
        # 净利口径：先把 LP 的获取成本扣掉，再拿"每 LP 净收益"排序
        rank_cost = ctx.lp_cost_rank if ctx.lp_cost_rank is not None else ctx.lp_cost
        key = (m - lam * ctx.spend[i]) / o.lp_cost - rank_cost[i]
        if key > 0:
            heapq.heappush(heap, (-key, next(seq), i))

    for i, o in enumerate(ctx.offers):
        if ctx.caps[i] <= 0 or o.lp_cost <= 0:
            continue
        if lp_left.get(o.corporation_id, 0) < o.lp_cost:
            continue
        if isk_left < ctx.spend[i]:
            continue
        push(i, 1)

    while heap:
        _neg, _s, i = heapq.heappop(heap)
        o = ctx.offers[i]
        n = copies[i] + 1
        if n > ctx.caps[i]:
            continue
        # LP / ISK 只减不增：现在买不起，之后也买不起
        if lp_left.get(o.corporation_id, 0) < o.lp_cost:
            continue
        if isk_left < ctx.spend[i]:
            continue

        copies[i] = n
        lp_left[o.corporation_id] = lp_left.get(o.corporation_id, 0.0) - o.lp_cost
        isk_left -= ctx.spend[i]

        if n < ctx.caps[i] and lp_left.get(o.corporation_id, 0) >= o.lp_cost:
            push(i, n + 1)

    total_lp = sum(copies[i] * ctx.offers[i].lp_cost for i in range(len(copies)))
    total_spend = sum(copies[i] * ctx.spend[i] for i in range(len(copies)))
    total_profit = sum(
        _row_profit(ctx, i, copies[i], broker_fee, listing)
        for i in range(len(copies)) if copies[i] > 0
    )
    stats = {
        "lp": float(total_lp),
        "spend": float(total_spend),
        "profit": float(total_profit),
        "lpLeft": lp_left,
        "iskLeft": float(isk_left) if isk_budget is not None else None,
    }
    return copies, stats


def _row_profit(ctx: _Ctx, i: int, n: int, broker_fee: float, listing: bool) -> float:
    if n <= 0:
        return 0.0
    o = ctx.offers[i]
    gross = ctx.curves[i].revenue(n * o.quantity)
    income = gross * ctx.tax[i]
    fee = income * broker_fee if listing else 0.0
    return income - fee - n * (o.isk_cost + o.material_cost)


def _row_gross(ctx: _Ctx, i: int, n: int) -> float:
    if n <= 0:
        return 0.0
    return ctx.curves[i].revenue(n * ctx.offers[i].quantity)


def _find_lambda(ctx: _Ctx, budget: float, broker_fee: float, listing: bool,
                 lam_hi: float = 1.0) -> float:
    """二分 ISK 的影子价格 λ，使"不考虑预算上限"时的总支出 ≈ 预算。"""
    if budget <= 0:
        return 0.0
    _, s0 = _solve(ctx, isk_budget=None, broker_fee=broker_fee, listing=listing, lam=0.0)
    if s0["spend"] <= budget * 1.001:
        return 0.0

    lo, hi = 0.0, max(lam_hi, 1e-6)
    for _ in range(24):
        _, s = _solve(ctx, isk_budget=None, broker_fee=broker_fee, listing=listing, lam=hi)
        if s["spend"] <= budget:
            break
        hi *= 2.0
    for _ in range(30):
        mid = (lo + hi) / 2.0
        _, s = _solve(ctx, isk_budget=None, broker_fee=broker_fee, listing=listing, lam=mid)
        if s["spend"] > budget:
            lo = mid
        else:
            hi = mid
    return hi


def _solve_for_budget(ctx: _Ctx, budget: float, broker_fee: float, listing: bool,
                      lam_hi: float = 1.0) -> tuple[list[int], dict, float]:
    lam = _find_lambda(ctx, budget, broker_fee, listing, lam_hi)
    copies, stats = _solve(ctx, isk_budget=budget, broker_fee=broker_fee, listing=listing, lam=lam)
    return copies, stats, lam


def _frontier(ctx: _Ctx, budget: float, broker_fee: float, listing: bool) -> list[dict]:
    """效率前沿：ISK 预算从 10% 扫到 160%，看利润的边际递减点。

    每个预算点都用与主计划相同的 λ 方法求解，保证曲线与计划自洽。
    """
    out: list[dict] = []
    if budget <= 0:
        return out
    lam_hi = 1.0
    for ratio in (0.1, 0.2, 0.3, 0.5, 0.7, 1.0, 1.3, 1.6):
        b = budget * ratio
        _, s, lam = _solve_for_budget(ctx, b, broker_fee, listing, lam_hi)
        lam_hi = max(lam, 1e-6)
        out.append({
            "budget": round(b, 2),
            "profit": round(s["profit"], 2),
            "spend": round(s["spend"], 2),
            "lp": round(s["lp"], 2),
        })
    return out


def build_plan(
    offers: list[Offer],
    lp_budgets: dict[int, float],
    isk_budget: float,
    *,
    market_share: float = 0.05,
    own_materials: bool = False,
    broker_fee: float = 0.0,
    listing: bool = False,
    min_profit_share: float = 0.01,
    min_daily_sales: float = 50_000_000.0,
    exclude_item_ids: list[int] | None = None,
    max_lp_share: float = 0.25,
    min_items: int = 5,
    impact: float = DEFAULT_IMPACT,
    lp_unit_costs: dict[int, float] | None = None,
    lp_cost_in_ranking: bool = True,
    sort_mode: str = "lp",
) -> dict:
    """生成兑换计划。

    参数
    ----
    market_share : 每天最多消化市场日成交量的比例（0 = 不限量；仅用于提示与候选表）
    sort_mode    : 档位口径——
        "lp"      LP 优先：按「每 LP 净利润」（利润/LP − LP 成本）排序
        "speed"   速度优先：按「每 LP 日成交额」（TxnAmount / LP）排序，只看市场规模
        "balanced"均衡：两者归一化后几何平均
    own_materials: 材料已自备 → 不计入 ISK 支出（利润仍扣材料成本）
    broker_fee   : 经纪人费率（0~1），仅在挂卖单（listing=True）时收取
    listing      : True = 挂卖单出售；False = 直接卖给买单（只付销售税）
    min_profit_share : 收益占比下限，低于该比例的商品被剔除并重分配
    min_daily_sales  : 最低日销售额（ISK/天），0 = 不限
    exclude_item_ids : 手动排除的物品 ID（按物品排除，跨公司生效）
    max_lp_share : 单个 offer 最多消耗该公司 LP 的比例（硬约束，0 = 不限）
    min_items    : 期望的最少商品种类数（仅用于诊断提示，不做硬约束）
    lp_unit_costs: {公司 ID: 每点 LP 的 ISK 获取成本}，参与排序与止损判断
        （仅用于「净利润」折算；lp_cost_in_ranking=False 时不影响排序/止损）
    lp_cost_in_ranking: False = 把 LP 当成已经拥有的沉没成本，
        排序只看毛利，尽量把 LP 用光（结果里仍会显示折算后的净利润）
    """
    broker_fee = min(1.0, max(0.0, broker_fee))
    lp_unit_costs = lp_unit_costs or {}
    manual_excluded = set(exclude_item_ids or [])

    def sales_ok(o: Offer) -> bool:
        return min_daily_sales <= 0 or o.txn_amount >= min_daily_sales

    all_valid = [o for o in offers if o.profit > 0 and o.lp_cost > 0]
    if not all_valid:
        return {"rows": [], "summary": None, "candidates": [], "frontier": [], "diagnostics": None}

    excluded = set(manual_excluded) | {o.item_id for o in all_valid if not sales_ok(o)}

    def make_ctx(keep: set[int]) -> _Ctx:
        """keep：参与本次求解的 offer（按 offer_id 记账）。"""
        chosen = [o for o in all_valid if o.offer_id in keep]
        curves = [PriceCurve(o, impact=impact) for o in chosen]
        spend = [o.isk_cost if own_materials else o.spend for o in chosen]
        tax = [o.tax_factor for o in chosen]
        caps = [
            _lp_share_cap(o, lp_budgets.get(o.corporation_id, 0.0), max_lp_share)
            for o in chosen
        ]
        display_cost = [float(lp_unit_costs.get(o.corporation_id, 0.0)) for o in chosen]
        return _Ctx(
            offers=chosen,
            curves=curves,
            spend=spend,
            caps=caps,
            tax=tax,
            lp_budgets=lp_budgets,
            lp_cost=display_cost,
            lp_cost_rank=display_cost if lp_cost_in_ranking else [0.0] * len(chosen),
            sort_score=_sort_scores(chosen, display_cost, sort_mode),
        )

    def run(keep: set[int]) -> tuple[_Ctx, list[int], dict, float]:
        ctx = make_ctx(keep)
        if ctx.sort_score is not None:
            # 偏好分模式不参与 ISK 影子价格（λ），预算直接当硬上限
            copies, stats = _solve(ctx, isk_budget=isk_budget, broker_fee=broker_fee,
                                   listing=listing, lam=0.0)
            return ctx, copies, stats, 0.0
        copies, stats, lam = _solve_for_budget(ctx, isk_budget, broker_fee, listing)
        return ctx, copies, stats, lam

    keep = {o.offer_id for o in all_valid if o.item_id not in excluded}
    if not keep:
        return {"rows": [], "summary": None, "candidates": [], "frontier": [], "diagnostics": None}

    ctx, copies, stats, lam = run(keep)

    # ---- 收益占比过滤：剔除小尾巴并重分配 ----
    auto_removed: list[dict] = []
    for _ in range(60):
        total = sum(
            _row_profit(ctx, i, copies[i], broker_fee, listing)
            for i in range(len(copies))
        )
        if total <= 0:
            break
        bad = [
            i for i in range(len(copies))
            if copies[i] > 0 and _row_profit(ctx, i, copies[i], broker_fee, listing) < total * min_profit_share
        ]
        if not bad or len(bad) >= sum(1 for c in copies if c > 0):
            break
        for i in bad:
            keep.discard(ctx.offers[i].offer_id)
            auto_removed.append({
                "itemName": ctx.offers[i].item_name,
                "corporationName": ctx.offers[i].corporation_name,
            })
        if not keep:
            break
        ctx, copies, stats, lam = run(keep)

    rows = _build_rows(ctx, copies, broker_fee, listing, market_share)
    summary = _build_summary(ctx, copies, rows, stats, lam, isk_budget, broker_fee, listing,
                             lp_budgets, auto_removed, min_items, market_share)
    candidates = _build_candidates(ctx, excluded, min_daily_sales, copies, broker_fee, listing, market_share)
    frontier = _frontier(ctx, isk_budget, broker_fee, listing)

    return {
        "rows": rows,
        "summary": summary,
        "candidates": candidates,
        "frontier": frontier,
        "diagnostics": _build_diagnostics(ctx, rows, summary, isk_budget, lp_budgets, min_items),
    }


# ---------------------------------------------------------------- 输出组装

def _minmax_log(vals: list[float]) -> list[float]:
    logs = [math.log1p(max(v, 0.0)) for v in vals]
    lo, hi = min(logs), max(logs)
    if hi - lo < 1e-12:
        return [0.5] * len(vals)
    return [(x - lo) / (hi - lo) for x in logs]


def _sort_scores(offers: list[Offer], lp_cost: list[float], sort_mode: str) -> list[float] | None:
    """速度优先 / 均衡 的偏好分（0~1 越大越先拿）；LP 优先返回 None 走利润边际排序。"""
    if sort_mode == "lp" or not offers:
        return None
    # 每 LP 净利润（利润/LP − LP 成本）与 每 LP 日成交额（市场规模）
    lp_eff = [o.profit / o.lp_cost - c for o, c in zip(offers, lp_cost)]
    speed = [o.txn_amount / o.lp_cost for o in offers]
    z_lp = _minmax_log(lp_eff)
    z_sp = _minmax_log(speed)
    if sort_mode == "speed":
        # 纯市场规模排序，用净利分做极小权重的并列裁决
        return [0.01 + s + 1e-3 * e for s, e in zip(z_sp, z_lp)]
    # 均衡：几何平均，负净利直接判 0
    return [0.01 + (max(e, 0.0) * s) ** 0.5 for e, s in zip(z_lp, z_sp)]


def _build_rows(ctx: _Ctx, copies: list[int], broker_fee: float, listing: bool,
                market_share: float) -> list[dict]:
    rows: list[dict] = []
    for i, n in enumerate(copies):
        if n <= 0:
            continue
        o = ctx.offers[i]
        gross = _row_gross(ctx, i, n)
        income = gross * ctx.tax[i]
        profit = _row_profit(ctx, i, n, broker_fee, listing)
        total_qty = n * o.quantity
        realized = gross / total_qty if total_qty > 0 else 0.0
        lp_total = n * o.lp_cost
        sell_rate = o.velocity * (market_share if market_share > 0 else 1.0)
        rows.append({
            "corporationId": o.corporation_id,
            "corporationName": o.corporation_name,
            "offerId": o.offer_id,
            "itemId": o.item_id,
            "itemName": o.item_name,
            "copies": n,
            "perCopyQuantity": o.quantity,
            "totalQuantity": total_qty,
            "lpCost": lp_total,
            "iskCost": round(n * o.isk_cost, 2),
            "materialCost": round(n * o.material_cost, 2),
            "spend": round(n * ctx.spend[i], 2),
            "unitPrice": round(o.price, 2),
            "realizedPrice": round(realized, 2),
            "priceImpact": round(realized / o.price - 1.0, 4) if o.price > 0 else 0.0,
            "income": round(income, 2),
            "grossIncome": round(gross, 2),
            "brokerFee": round(income * broker_fee if listing else 0.0, 2),
            "profit": round(profit, 2),
            "netProfit": round(profit - ctx.lp_cost[i] * lp_total, 2),
            "unitProfit": round(profit / lp_total, 2) if lp_total else 0.0,
            "lpUnitCost": round(ctx.lp_cost[i], 2),
            "netIsPerLp": round(profit / lp_total - ctx.lp_cost[i], 2) if lp_total else 0.0,
            "marginalIsPerLp": round(_marginal_per_lp(ctx, i, n, broker_fee, listing), 2),
            "saleIndex": o.sale_index,
            "txnAmount": round(o.txn_amount, 2),
            "volume": o.volume,
            "velocity": round(o.velocity, 2),
            "dailyFlow": round(profit / max(total_qty / o.velocity, 1.0), 2) if o.velocity > 0 else round(profit, 2),
            "sellDays": round(total_qty / sell_rate, 1) if sell_rate > 0 else None,
            "hasCurve": ctx.curves[i].has_curve,
            "flags": list(o.flags),
            "materials": [
                {"name": m["name"], "quantity": m["quantity"], "isBlueprint": m["isBlueprint"]}
                for m in o.materials
            ],
        })
    rows.sort(key=lambda r: r["profit"], reverse=True)
    return rows


def _marginal_per_lp(ctx: _Ctx, i: int, n: int, broker_fee: float, listing: bool) -> float:
    o = ctx.offers[i]
    if n <= 0 or o.lp_cost <= 0:
        return 0.0
    gross = ctx.curves[i].margin((n - 1) * o.quantity, n * o.quantity)
    income = gross * ctx.tax[i]
    profit = income * ((1.0 - broker_fee) if listing else 1.0) - (o.isk_cost + o.material_cost)
    return profit / o.lp_cost


def _build_summary(ctx, copies, rows, stats, lam, isk_budget, broker_fee, listing,
                   lp_budgets, auto_removed, min_items, market_share) -> dict:
    total_lp = sum(r["lpCost"] for r in rows)
    total_spend = sum(r["spend"] for r in rows)
    total_income = sum(r["income"] for r in rows)
    total_profit = sum(r["profit"] for r in rows)
    lp_remaining = {str(c): round(float(v), 2) for c, v in stats["lpLeft"].items()}
    isk_remaining = stats["iskLeft"] if stats["iskLeft"] is not None else max(0.0, isk_budget - total_spend)

    # 瓶颈判定：看谁是紧约束（剩余 < 2% 视为用尽）
    lp_total_budget = sum(float(v) for v in lp_budgets.values()) or 1.0
    lp_left_total = sum(float(v) for v in stats["lpLeft"].values())
    rank_cost_used = bool(ctx.lp_cost_rank and any(c > 0 for c in ctx.lp_cost_rank))
    lp_used_ratio = 1.0 - lp_left_total / lp_total_budget
    isk_used_ratio = total_spend / isk_budget if isk_budget > 0 else 0.0
    if isk_used_ratio >= 0.95 and lp_used_ratio < 0.95:
        bottleneck = "ISK"
    elif lp_used_ratio >= 0.95 and isk_used_ratio < 0.95:
        bottleneck = "LP"
    elif lp_used_ratio >= 0.95 and isk_used_ratio >= 0.95:
        bottleneck = "BOTH"
    else:
        bottleneck = "NONE"

    avg_spend_per_lp = total_spend / total_lp if total_lp > 0 else 0.0
    warnings: list[str] = []
    if auto_removed:
        names = "、".join(r["itemName"] for r in auto_removed[:5])
        more = "…" if len(auto_removed) > 5 else ""
        warnings.append(f"已剔除 {len(auto_removed)} 个收益占比过小的商品（LP / ISK 已重新分配）：{names}{more}")
    if lp_left_total >= 1 and bottleneck == "ISK":
        need = lp_left_total * avg_spend_per_lp
        warnings.append(
            f"⛔ 瓶颈是 ISK：你的本金只够消耗 {total_lp:,.0f} LP（占 {lp_used_ratio * 100:.0f}%），"
            f"剩余 {lp_left_total:,.0f} LP 需要再投入约 {need / 1e9:.2f}B ISK 才能吃满"
        )
    elif lp_left_total >= 1:
        if rank_cost_used:
            unit = max(ctx.lp_cost_rank)
            warnings.append(
                f"{lp_left_total:,.0f} LP 未使用：剩下的商品每点 LP 赚不回 {unit:,.0f} ISK 的获取成本"
                "（想全部用光可在「设置」里取消勾选「LP 视为已有」）"
            )
        else:
            warnings.append(f"{lp_left_total:,.0f} LP 未使用：剩余商品在流动性 / 单商品占比上限内已无利可图")
    if len(rows) < min_items and rows:
        if rank_cost_used:
            warnings.append(
                f"当前只覆盖 {len(rows)} 种商品（目标 {min_items} 种）：多数商品扣掉 LP 获取成本后不赚钱，"
                "可降低 LP 成本或勾选「LP 视为已有」"
            )
        else:
            warnings.append(
                f"当前只覆盖 {len(rows)} 种商品（目标 {min_items} 种）：受单商品占比上限限制，"
                "可放宽「单品 LP 上限」或降低「最低日销售额」"
            )
    if lam > 0:
        warnings.append(
            f"ISK 影子价格 λ = {lam:.4f}：每多投入 1 亿 ISK，预计多赚 {lam * 1e8:,.0f} ISK"
        )

    # 组合层面的「速度」：每 LP 能站进多大的市场 / 平均几天卖完（按 LP 加权）
    lp_w = sum(r["lpCost"] for r in rows) or 1.0
    avg_throughput = (
        sum((r["txnAmount"] or 0.0) / max(r["lpCost"] / max(r["copies"], 1), 1e-9) * r["lpCost"]
            for r in rows) / lp_w
    ) if rows else 0.0
    avg_days = (sum((r.get("sellDays") or 0.0) * r["lpCost"] for r in rows) / lp_w) if rows else 0.0

    return {
        "totalLp": round(total_lp, 2),
        "totalSpend": round(total_spend, 2),
        "totalIncome": round(total_income, 2),
        "totalProfit": round(total_profit, 2),
        "items": len(rows),
        "lpRemaining": lp_remaining,
        "iskRemaining": round(max(0.0, isk_remaining), 2),
        "avgIsPerLp": round(total_profit / total_lp, 2) if total_lp > 0 else 0.0,
        "avgThroughputPerLp": round(avg_throughput, 2),
        "avgSellDays": round(avg_days, 2),
        "avgSpendPerLp": round(avg_spend_per_lp, 2),
        "profitAfterLpCost": round(
            total_profit - sum(ctx.lp_cost[i] * copies[i] * ctx.offers[i].lp_cost
                               for i in range(len(copies))), 2),
        "lpUnitCostTotal": round(sum(ctx.lp_cost[i] * copies[i] * ctx.offers[i].lp_cost
                                     for i in range(len(copies))), 2),
        "bottleneck": bottleneck,
        "iskShadowPrice": round(lam, 6),
        "marginalProfitPer100m": round(lam * 1e8, 2),
        "brokerFeeTotal": round(sum(r["brokerFee"] for r in rows), 2),
        "lpUsedRatio": round(lp_used_ratio, 4),
        "iskUsedRatio": round(isk_used_ratio, 4),
        "warnings": warnings,
    }


def _build_candidates(ctx: _Ctx, excluded: set[int], min_daily_sales: float,
                      copies: list[int], broker_fee: float, listing: bool,
                      market_share: float) -> list[dict]:
    out: list[dict] = []
    for i, o in enumerate(ctx.offers):
        first_marginal = _marginal_per_lp(ctx, i, 1, broker_fee, listing)
        out.append({
            "corporationId": o.corporation_id,
            "corporationName": o.corporation_name,
            "offerId": o.offer_id,
            "itemId": o.item_id,
            "itemName": o.item_name,
            "perCopyQuantity": o.quantity,
            "lpCost": o.lp_cost,
            "iskCost": o.isk_cost,
            "materialCost": o.material_cost,
            "unitPrice": round(o.price, 2),
            "profit": round(o.profit, 2),
            "unitProfit": round(o.unit_profit, 2),
            "marginIsPerLp": round(first_marginal, 2),
            "unitItemProfit": round(o.unit_item_profit, 2),
            "saleIndex": o.sale_index,
            "volume": o.volume,
            "velocity": round(o.velocity, 2),
            "dailyFlow": round(o.unit_item_profit * o.velocity * (market_share or 1.0), 2),
            "hasCurve": ctx.curves[i].has_curve,
            "flags": list(o.flags),
            "chosen": copies[i] > 0,
            "excluded": o.item_id in excluded,
            "filtered": (min_daily_sales > 0 and o.txn_amount < min_daily_sales) or o.item_id in excluded,
        })
    out.sort(key=lambda r: r["marginIsPerLp"], reverse=True)
    return out


def _build_diagnostics(ctx: _Ctx, rows: list[dict], summary: dict, isk_budget: float,
                       lp_budgets: dict[int, float], min_items: int) -> dict:
    """面向操作的建议：瓶颈、单商品集中度、下一步该做什么。"""
    total_lp = summary.get("totalLp", 0) or 1.0
    top = max(rows, key=lambda r: r["lpCost"], default=None)
    top_share = (top["lpCost"] / total_lp) if top else 0.0
    hints: list[str] = []
    if summary.get("bottleneck") == "ISK":
        hints.append("先补本金或减少 LP 库存：当前 ISK 是唯一瓶颈，再刷 LP 也不会提高收益。")
    elif summary.get("bottleneck") == "LP":
        hints.append("LP 已打满：继续提升收益只能换更多 LP 或等价格更好。")
    if top_share > 0.5 and len(rows) > 1:
        hints.append(
            f"「{top['itemName']}」占了 {top_share * 100:.0f}% 的计划 LP，"
            "若它的价格曲线陡降，整份计划会被拖累——可降低单商品占比上限。"
        )
    no_curve = sum(1 for r in rows if not r.get("hasCurve"))
    if no_curve:
        hints.append(f"{no_curve} 个商品没有可用的价格曲线，用的是线性衰减模型，误差较大。")
    return {
        "bottleneck": summary.get("bottleneck"),
        "iskShadowPrice": summary.get("iskShadowPrice", 0.0),
        "topItemShare": round(top_share, 4),
        "topItem": top["itemName"] if top else None,
        "items": len(rows),
        "minItems": min_items,
        "hints": hints,
    }
