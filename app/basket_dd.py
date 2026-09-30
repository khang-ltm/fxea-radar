"""Rebuild what an EA's open trades were worth, minute by minute, after the fact.

The drawdown that matters for a grid happens while its basket is open, and
nothing records it: MT5 keeps the account's equity curve, not each EA's, and by
the time the basket closes the only trace left is a row of deals that all closed
at the same second. Sampling answers it from now on. This answers it backwards.

Everything needed is already in the terminal. Each closed position knows when it
opened, at what price, which way and how big; each symbol has its M1 bars. Walk
the minutes, price every position that was open in that minute at the worst the
minute went, add what had already been banked, and that is the curve the account
actually travelled - including the part nobody was watching.

It is a reconstruction, and it says so. A minute's worst price is taken from its
high and low, so the low point is the worst the basket could have been at any
tick inside that minute, not necessarily a price that all positions saw
together. That errs deep rather than shallow, deliberately: a drawdown figure
that flatters is worse than useless.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

M1 = 1
POSITION_BUY = 0


def _rate_span(mt5, symbol: str, start: datetime, end: datetime):
    try:
        return mt5.copy_rates_range(symbol, getattr(mt5, "TIMEFRAME_M1", M1),
                                    start, end) or []
    except Exception:                                          # noqa: BLE001
        return []


def _per_point(mt5, symbol: str, is_buy: bool, volume: float, entry: float):
    """What one point of price movement is worth on this position, in account money.

    Asked of MT5 rather than derived: contract sizes, tick values and the
    conversion of a quote currency back into the account's are exactly the
    arithmetic that is easy to get subtly wrong, and order_calc_profit is the
    terminal's own answer. One call per position, then simple scaling.
    """
    info = mt5.symbol_info(symbol)
    step = getattr(info, "point", 0) or 0.00001
    kind = (getattr(mt5, "ORDER_TYPE_BUY", 0) if is_buy
            else getattr(mt5, "ORDER_TYPE_SELL", 1))
    try:
        moved = mt5.order_calc_profit(kind, symbol, volume, entry, entry + step)
    except Exception:                                          # noqa: BLE001
        moved = None
    if moved is None:
        return None, step
    return float(moved), step


def reconstruct(mt5, positions: list, closes: list, hours: int = 24,
                now: datetime | None = None) -> dict:
    """The worst the account stood on these positions, minute by minute.

    `positions` are dicts of symbol, type, volume, entry price, opened_at and
    closed_at (epoch seconds; closed_at None means still open). `closes` are
    (epoch, net) for money already banked inside the window.
    """
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    inside = [p for p in positions
              if (p.get("closed_at") or now.timestamp()) >= start.timestamp()]
    if not inside:
        return {"ok": False, "reason": "no positions in the window"}

    # one price series per symbol, one point-value per position
    bars: dict[str, dict] = {}
    for symbol in {p["symbol"] for p in inside}:
        rows = _rate_span(mt5, symbol, start, now + timedelta(minutes=1))
        if len(rows) == 0:
            return {"ok": False, "reason": f"no M1 history for {symbol}"}
        bars[symbol] = {int(r["time"]): (float(r["high"]), float(r["low"])) for r in rows}

    priced = []
    for p in inside:
        value, step = _per_point(mt5, p["symbol"], p["type"] == POSITION_BUY,
                                 float(p["volume"]), float(p["entry"]))
        if value is None:
            return {"ok": False, "reason": f"MT5 would not price {p['symbol']}"}
        priced.append({**p, "per_point": value, "step": step})

    minutes = sorted({t for sym in bars for t in bars[sym]})
    if not minutes:
        return {"ok": False, "reason": "no minutes to walk"}

    banked, at, series = 0.0, 0, []
    ordered = sorted(closes, key=lambda c: c[0])
    for minute in minutes:
        while at < len(ordered) and ordered[at][0] <= minute:
            banked = round(banked + ordered[at][1], 2)
            at += 1
        floating = 0.0
        for p in priced:
            opened = p["opened_at"]
            closed = p.get("closed_at") or float("inf")
            if not (opened <= minute + 59 and closed >= minute):
                continue
            high, low = bars[p["symbol"]].get(minute, (None, None))
            if high is None:
                continue
            # the worst this position could have stood inside the minute
            worst = low if p["type"] == POSITION_BUY else high
            floating += (worst - p["entry"]) / p["step"] * p["per_point"]
        series.append((minute, round(banked + floating, 2)))

    peak, dd, trough = None, 0.0, None
    for _when, value in series:
        peak = value if peak is None else max(peak, value)
        if peak - value > dd:
            dd, trough = round(peak - value, 2), value
    return {"ok": True, "dd": dd, "worst": round(min(v for _t, v in series), 2),
            "trough": trough, "minutes": len(series),
            "positions": len(priced), "from": minutes[0]}
