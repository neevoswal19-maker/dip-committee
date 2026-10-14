"""US stock transaction costs through Groww (GIFT City, cleared by Alpaca).

Per order, from groww.in/pricing/us-stocks (October 2026):

  Brokerage    0.1% of the order value, at least $1
  GST          18% on the brokerage
  SEC fee      0.00206% of the order value, on SELLs only
  FINRA TAF    $0.000195 a share on SELLs, at most $9.79 an order
  CAT fee      $0.000003 a share, both sides (rounds to nothing)

and one cost Groww does not publish: converting rupees to dollars and back,
which happens at the remitting bank's rate. Banks typically mark up 0.5-1%
each way. It is modelled here as a percentage of every order, which is
conservative for someone who leaves dollars in the account between trades.

Rates live in config.yaml under `us.costs`, like the Indian ones.
"""

from __future__ import annotations

from typing import Any

from src.charges import ChargeBreakdown, Side
from src.config import load_config


def estimate(side: Side, quantity: float, price: float, *, cfg: Any = None,
             fx_pct: float | None = None) -> ChargeBreakdown:
    """All-in cost of one US order, in dollars."""
    cfg = cfg or load_config()
    value = float(quantity) * float(price)
    if value <= 0:
        return ChargeBreakdown()

    brokerage = max(value * float(cfg.get("us.costs.brokerage_pct", 0.1)) / 100.0,
                    float(cfg.get("us.costs.brokerage_min_usd", 1.0)))
    gst = brokerage * float(cfg.get("us.costs.gst_pct", 18.0)) / 100.0

    regulatory = float(quantity) * float(cfg.get("us.costs.cat_per_share_usd", 0.000003))
    if side == "SELL":
        regulatory += value * float(cfg.get("us.costs.sec_fee_pct", 0.00206)) / 100.0
        regulatory += min(float(quantity) * float(cfg.get("us.costs.finra_per_share_usd", 0.000195)),
                          float(cfg.get("us.costs.finra_max_usd", 9.79)))

    fx = float(cfg.get("us.costs.fx_markup_pct", 0.5)) if fx_pct is None else float(fx_pct)
    conversion = value * fx / 100.0

    return ChargeBreakdown(brokerage=brokerage, gst=gst, exchange_fee=regulatory, other=conversion)


def cost_fn(cfg: Any = None, fx_pct: float | None = None):
    """A simulator cost hook: (side, quantity, price) -> dollars."""
    cfg = cfg or load_config()
    return lambda side, quantity, price: estimate(side, quantity, price, cfg=cfg, fx_pct=fx_pct).total
