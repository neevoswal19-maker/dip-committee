"""Indian delivery-equity transaction costs.

Used to prefill the trade form so you only type price and quantity, then
overwrite anything the contract note disagrees with. The estimate is close
but never authoritative - brokers round differently, DP charges vary by
depository, and rates move with each budget.

Every rate lives in config.yaml rather than here, because these change and a
number buried in code is a number nobody updates.

Delivery equity in India, as of the rates in config:

  Brokerage     Groww: the lower of Rs 20 or 0.1% of turnover, per order
  STT           0.1% on both buy and sell
  Exchange fee  NSE transaction charge on turnover
  SEBI fee      turnover fee
  Stamp duty    0.015%, on the BUY only
  GST           18%, on brokerage + exchange fee + SEBI fee only
  DP charge     flat, on the SELL only - the depository's fee for debiting
                shares from your demat account

The two asymmetries matter and are easy to get wrong: stamp duty is charged
on purchases only, and the DP charge on sales only. GST applies to the
service fees, never to STT or stamp duty, which are taxes in their own right.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Literal

from src.config import load_config

Side = Literal["BUY", "SELL"]


@dataclass
class ChargeBreakdown:
    """Every component, itemised the way a contract note itemises them."""

    brokerage: float = 0.0
    stt: float = 0.0
    exchange_fee: float = 0.0
    sebi_fee: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    dp_charge: float = 0.0
    other: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.stt + self.exchange_fee + self.sebi_fee
            + self.stamp_duty + self.gst + self.dp_charge + self.other,
            2,
        )

    def to_dict(self) -> dict[str, float]:
        payload = {k: round(v, 2) for k, v in asdict(self).items()}
        payload["total"] = self.total
        return payload


def estimate(
    side: Side,
    quantity: float,
    price: float,
    *,
    cfg: Any = None,
    broker: str | None = None,
) -> ChargeBreakdown:
    """Estimate the all-in cost of one delivery-equity order.

    Returns zeroes for a non-positive turnover rather than raising, so the
    form can call this on every keystroke while a field is still empty.
    """
    cfg = cfg or load_config()
    turnover = float(quantity) * float(price)

    if turnover <= 0:
        return ChargeBreakdown()

    is_buy = str(side).upper() == "BUY"
    rates = cfg.get("charges", {}) or {}
    broker = (broker or rates.get("default_broker", "groww")).lower()
    broker_rates = (rates.get("brokers", {}) or {}).get(broker, {}) or {}

    # Brokerage: a percentage capped at a flat maximum, per order.
    pct = float(broker_rates.get("delivery_brokerage_pct", 0.0))
    cap = float(broker_rates.get("delivery_brokerage_max", 20.0))
    brokerage = min(turnover * pct / 100.0, cap) if pct > 0 else 0.0

    stt = turnover * float(rates.get("stt_pct", 0.1)) / 100.0
    exchange_fee = turnover * float(rates.get("exchange_txn_pct", 0.00297)) / 100.0
    sebi_fee = turnover * float(rates.get("sebi_turnover_pct", 0.0001)) / 100.0

    # Stamp duty on purchases only; DP charge on sales only.
    stamp_duty = turnover * float(rates.get("stamp_duty_pct", 0.015)) / 100.0 if is_buy else 0.0
    dp_charge = 0.0 if is_buy else float(broker_rates.get("dp_charge", rates.get("dp_charge", 20.0)))

    # GST applies to the service fees, not to STT or stamp duty - those are
    # taxes themselves and are not taxed again.
    gst = (brokerage + exchange_fee + sebi_fee) * float(rates.get("gst_pct", 18.0)) / 100.0

    return ChargeBreakdown(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_fee=round(exchange_fee, 2),
        sebi_fee=round(sebi_fee, 2),
        stamp_duty=round(stamp_duty, 2),
        gst=round(gst, 2),
        dp_charge=round(dp_charge, 2),
    )


def net_amount(side: Side, quantity: float, price: float, total_charges: float) -> float:
    """Cash that actually moves.

    A purchase costs turnover plus charges; a sale returns turnover minus
    charges. This is the figure returns should be measured against, not the
    gross turnover.
    """
    turnover = float(quantity) * float(price)
    if str(side).upper() == "BUY":
        return round(turnover + float(total_charges), 2)
    return round(turnover - float(total_charges), 2)


def effective_price(side: Side, quantity: float, price: float, total_charges: float) -> float:
    """Per-share price once charges are folded in.

    The honest cost basis: a purchase is effectively dearer than the traded
    price, a sale effectively cheaper. Using this for FIFO cost basis is what
    keeps reported returns from flattering themselves.
    """
    if quantity <= 0:
        return 0.0
    return round(net_amount(side, quantity, price, total_charges) / float(quantity), 4)


def round_trip_cost_pct(quantity: float, price: float, *, cfg: Any = None) -> float:
    """Buy plus sell charges as a percentage of turnover.

    Useful for sizing sanity: on a small position the round trip can eat a
    meaningful slice of the expected move, which is the reason for the
    minimum position size in the sizer.
    """
    turnover = float(quantity) * float(price)
    if turnover <= 0:
        return 0.0
    buy = estimate("BUY", quantity, price, cfg=cfg).total
    sell = estimate("SELL", quantity, price, cfg=cfg).total
    return round((buy + sell) / turnover * 100.0, 3)
