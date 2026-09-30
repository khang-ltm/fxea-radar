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
    """Minute bars for a window, asked for three ways.

    copy_rates_range answers nothing for a symbol the terminal has not selected,
    and nothing again when it holds the bars but not for that exact span - so a
    plain call returning empty means "ask differently", not "no history". The
    symbol is selected first, the range is tried, and a count-back from the end
    is tried after that.
    """
    tf = getattr(mt5, "TIMEFRAME_M1", M1)
    try:
        mt5.symbol_select(symbol, True)
    except Exception:                                          # noqa: BLE001
        pass

    for attempt in ("range", "from", "pos"):
        try:
            if attempt == "range":
                rows = mt5.copy_rates_range(symbol, tf, start, end)
            elif attempt == "from":
                minutes = max(60, int((end - start).total_seconds() // 60) + 5)
                rows = mt5.copy_rates_from(symbol, tf, end, minutes)
            else:
                minutes = max(60, int((end - start).total_seconds() // 60) + 5)
                rows = mt5.copy_rates_from_pos(symbol, tf, 0, minutes)
        except Exception:                                      # noqa: BLE001
            rows = None
        if rows is not None and len(rows) > 0:
            start_s, end_s = start.timestamp(), end.timestamp()
            return [r for r in rows if start_s <= int(r["time"]) <= end_s] or list(rows)
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
            why = ""
            try:
                why = f" ({mt5.last_error()})"
            except Exception:                                  # noqa: BLE001
                pass
            return {"ok": False, "reason": f"MT5 returned no M1 bars for {symbol}{why}"}
        bars[symbol] = {int(r["time"]): (float(r["high"]), float(r["low"])) for r in rows}

    priced = []
    for p in inside:
        value, step = _per_point(mt5, p["symbol"], p["type"] == POSITION_BUY,
                                 float(p["volume"]), float(p["entry"]))
        if value is None:
            return {"ok": False, "reason": f"MT5 would not price {p['symbol']}"}
        priced.append({**p, "per_point": value, "step": step})

    # Only the minutes something was open matter, and over a month that is a
    # small fraction of them: walking every minute of thirty days for an EA that
    # held trades for six hours is work nobody needs done.
    live_minutes = set()
    for p in priced:
        first = int(p["opened_at"]) // 60 * 60
        last = int(p.get("closed_at") or now.timestamp()) // 60 * 60
        held = bars.get(p["symbol"]) or {}
        live_minutes |= {t for t in held if first <= t <= last}
    minutes = sorted(live_minutes)
    if not minutes:
        return {"ok": False, "reason": "no minutes where a trade was open"}

    # Two series, because they answer different questions. The combined one
    # says what the account was carrying overall; the open-only one says how
    # far underwater this EA's positions themselves went, which is the number
    # that does not quietly improve just because the EA banked profit earlier
    # in the window.
    floating_only = []
    banked, at, series = 0.0, 0, []
    ordered = sorted(closes, key=lambda c: c[0])
    # money banked before the first watched minute is already part of the curve
    while ordered and minutes and ordered[0][0] < minutes[0]:
        banked = round(banked + ordered.pop(0)[1], 2)
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
        floating_only.append((minute, round(floating, 2)))

    peak, dd, trough = None, 0.0, None
    for _when, value in series:
        peak = value if peak is None else max(peak, value)
        if peak - value > dd:
            dd, trough = round(peak - value, 2), value
    # Can the model reproduce what already happened? Every closed position has
    # a real result, and pricing it at the minute it closed should land near
    # that number. Where it does not, the rebuild is describing some other
    # hour or some other contract - and a drawdown from it is a guess wearing
    # a decimal point, which must not be shown as a measurement.
    checked, drift, worst_miss = 0, 0.0, 0.0
    for p in priced:
        actual, closed_at = p.get("profit"), p.get("closed_at")
        if actual is None or not closed_at:
            continue
        bar = (bars.get(p["symbol"]) or {}).get(int(closed_at) // 60 * 60)
        if bar is None:
            continue
        high, low = bar
        predicted = ((high + low) / 2 - p["entry"]) / p["step"] * p["per_point"]
        miss = abs(predicted - float(actual))
        checked += 1
        drift += miss
        worst_miss = max(worst_miss, round(miss, 2))
    off_by = round(drift / checked, 2) if checked else None

    earliest_open = min(int(p["opened_at"]) for p in priced)
    return {"ok": True, "dd": dd,
            # the deepest its own open trades stood, banked profit excluded
            "worst": round(min(v for _t, v in floating_only), 2),
            "worst_with_banked": round(min(v for _t, v in series), 2),
            "trough": trough, "minutes": len(series),
            "positions": len(priced), "from": minutes[0],
            # how far back the bars actually reached, and whether that covers
            # the whole life of these positions
            "bars_from": minutes[0], "first_trade": earliest_open,
            "partial": earliest_open < minutes[0] - 60,
            # and whether the model agrees with trades it can check
            "checked": checked, "off_by": off_by, "worst_miss": worst_miss,
            "trusted": bool(checked) and off_by is not None and off_by < 5}


def cycles(positions: list, now_ts: float) -> list:
    """Split an EA's positions into the stretches where it held anything.

    A grid's basket and a plain EA's single trade are the same thing measured
    the same way: from the first position opening to the last one closing, with
    a gap of flat in between marking the end of one and the start of the next.
    Nothing here cares which kind of EA it is - holding or not holding is the
    only distinction that matters, and the positions say it plainly.
    """
    spans = sorted(((int(p["opened_at"]), int(p.get("closed_at") or now_ts), p)
                    for p in positions if p.get("opened_at")),
                   key=lambda s: s[0])
    out: list = []
    for start, end, pos in spans:
        if out and start <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], end)
            out[-1]["positions"].append(pos)
        else:
            out.append({"start": start, "end": end, "positions": [pos]})
    return out


def last_cycle(mt5, positions: list, closes: list, now=None) -> dict:
    """What the most recent basket cost to hold, and what it paid.

    This is the honest headline for a grid: not a day's worth of arithmetic, but
    the last time it opened something, how far underwater that went before it
    ended, and what came out. For an EA that trades one position at a time it is
    simply that trade.
    """
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    runs = cycles(positions, now.timestamp())
    if not runs:
        return {"ok": False, "reason": "no positions to look at"}

    run = runs[-1]
    start = datetime.fromtimestamp(run["start"], tz=timezone.utc) - timedelta(minutes=1)
    end = (datetime.fromtimestamp(run["end"], tz=timezone.utc) + timedelta(minutes=1)
           if run["end"] < now.timestamp() else now)
    hours = max(1, int((end - start).total_seconds() // 3600) + 1)

    inside = [c for c in closes if run["start"] <= c[0] <= run["end"] + 60]
    out = reconstruct(mt5, run["positions"], inside, hours=hours, now=end)
    if not out.get("ok"):
        return out
    out.update({"started": run["start"], "ended": None if run["end"] >= now.timestamp()
                else run["end"],
                "open_now": run["end"] >= now.timestamp(),
                "trades": len(run["positions"]),
                "net": round(sum(c[1] for c in inside), 2),
                "cycles_seen": len(runs)})
    return out
