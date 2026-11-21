import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import enrich, planner, starweb

REGION_ID = 10000002


def offers_for(corp_id, name, tax=4.5):
    raw = starweb.offers(REGION_ID, corp_id, days=7, tax=tax, scope=0.05)
    offers = [planner.Offer.from_api(d, corp_id, name) for d in raw]
    enrich.enrich(offers, region_id=REGION_ID, days=7, tax=tax)
    return offers


def main():
    offers = offers_for(1000035, "加达里海军")
    print(f"offers: {len(offers)}, valid: {sum(1 for o in offers if o.profit > 0 and o.lp_cost > 0)}, "
          f"curves: {sum(1 for o in offers if len(o.curve_points or []) >= 2)}")
    lp = {1000035: 1_000_000}

    cases = [
        ("默认(share5%/3天/25%上限)", dict(market_share=0.05, sell_days=3)),
        ("不限占比(100%)", dict(market_share=0.05, sell_days=3, max_lp_share=0)),
        ("ISK=1B", dict(isk=1e9)),
        ("ISK=0.5B", dict(isk=0.5e9)),
        ("broker=2%", dict(listing=True, broker_fee=0.02)),
    ]
    for name, kw in cases:
        kw = dict(kw)
        isk = kw.pop("isk", 2e9)
        res = planner.build_plan(offers, lp, isk, min_daily_sales=2e8, **kw)
        s = res["summary"]
        if not s:
            print(f"{name}: no plan"); continue
        print(f"{name}: profit={s['totalProfit']/1e9:.3f}B lp={s['totalLp']:,.0f} "
              f"spend={s['totalSpend']/1e9:.2f}B avg={s['avgIsPerLp']:,.0f} "
              f"items={s['items']} bottleneck={s['bottleneck']} lam={s['iskShadowPrice']:.4f}")
        for r in res["rows"][:5]:
            print(f"   {r['itemName'][:24]:26s} x{r['copies']:<5d} lp={r['lpCost']:<9,.0f} "
                  f"profit={r['profit']/1e6:>10,.2f}M iskLp={r['unitProfit']:>8,.0f} "
                  f"impact={r['priceImpact']*100:>6.2f}% days={r['sellDays']}")
    print("frontier:", [(round(p['budget']/1e9, 2), round(p['profit']/1e9, 3)) for p in res["frontier"]])


if __name__ == "__main__":
    main()
