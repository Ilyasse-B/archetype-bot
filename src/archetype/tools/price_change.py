"""
price change for the coin in focus.

two feeds, neither is a tool the model calls:
- every think: how price moved since the last peek (any size, any speed)
- a fast move only: net market-cap change over about 10 seconds, pushed by the watcher

the long explanation is attached to the first fast alert for that coin.
later alerts are just the numbers. the first line stays in the history.
"""

from __future__ import annotations

from typing import Any

# one alert per window. net move across ~10s, not every 0.5s wiggle.
# 45k down to 3k inside that window is one alert. a 25% blip in 1s is not,
# until the 10s net is still at least this big.
DEFAULT_FAST_RULES: tuple[tuple[float, float], ...] = ((25.0, 4.0), (50.0, 8.0))
DEFAULT_FAST_MOVE_PCT = 50.0
DEFAULT_FAST_WINDOW_SEC = 10.0
MIN_FAST_MOVE_PCT = 25.0
# samples older than the window are not part of this alert
SAMPLE_KEEP_SEC = 10.0


def required_pct(elapsed: float, anchors: list[tuple[float, float]]) -> float:
    """
    how big a move has to be when it took `elapsed` seconds.

    straight line through the anchor points, extended both ways.
    25% at 4s and 50% at 8s means 6.25% per second, so 6s needs 37.5%.
    """
    pts = sorted((sec, pct) for pct, sec in anchors if sec > 0 and pct > 0)
    if not pts or elapsed <= 0:
        return 0.0
    if len(pts) == 1:
        sec, pct = pts[0]
        return pct * (elapsed / sec)
    (sec1, pct1), (sec2, pct2) = pts[0], pts[-1]
    if sec2 == sec1:
        return pct2
    slope = (pct2 - pct1) / (sec2 - sec1)
    return pct1 + slope * (elapsed - sec1)


def detect_fast_move(
    samples: list[tuple[float, float, float]],
    price: float,
    ts: float,
    *,
    rules: list[tuple[float, float]] | None = None,
    move_pct: float | None = None,
    window_sec: float | None = None,
    min_pct: float = MIN_FAST_MOVE_PCT,
) -> tuple[float, float, float, float] | None:
    """
    samples are (ts, price, fdv), oldest first, not including this tick.

    returns (from_price, from_fdv, elapsed_sec, change_pct) when the move
    is at least as fast as the line through the anchor points AND at least
    min_pct. a 7% blip in one second is over the speed line and still quiet.
    if several old ticks qualify, keep the biggest percent.
    """
    use = list(rules) if rules else []
    if not use and move_pct is not None and window_sec is not None:
        use = [(move_pct, window_sec)]
    if not use:
        use = list(DEFAULT_FAST_RULES)
    if price <= 0:
        return None
    best: tuple[float, float, float, float] | None = None
    for sample_ts, sample_price, sample_fdv in samples:
        if sample_price <= 0:
            continue
        elapsed = ts - sample_ts
        if elapsed <= 0:
            continue
        change = (price - sample_price) / sample_price * 100.0
        # floor first. 6.25% in 1s clears the line and is still noise.
        if abs(change) < min_pct:
            continue
        need = required_pct(elapsed, use)
        if need <= 0 or abs(change) < need:
            continue
        hit = (sample_price, sample_fdv, elapsed, change)
        if best is None or abs(change) > abs(best[3]):
            best = hit
    return best


def detect_window_move(
    samples: list[tuple[float, float, float]],
    price: float,
    ts: float,
    *,
    window_sec: float = DEFAULT_FAST_WINDOW_SEC,
    min_pct: float = MIN_FAST_MOVE_PCT,
) -> tuple[float, float, float, float] | None:
    """
    net change from the oldest sample to this tick.

    quiet until the span is at least window_sec, and quiet when the net
    move is under min_pct. one check, not every little swing inside the window.
    samples are (ts, price, fdv), oldest first, not including this tick.
    returns (from_price, from_fdv, elapsed_sec, change_pct).
    """
    if price <= 0 or window_sec <= 0 or not samples:
        return None
    anchor_ts, anchor_price, anchor_fdv = samples[0]
    if anchor_price <= 0:
        return None
    elapsed = ts - anchor_ts
    if elapsed < window_sec:
        return None
    change = (price - anchor_price) / anchor_price * 100.0
    if abs(change) < min_pct:
        return None
    return anchor_price, anchor_fdv, elapsed, change


def trim_samples(
    samples: list[tuple[float, float, float]],
    ts: float,
    *,
    keep_sec: float = SAMPLE_KEEP_SEC,
) -> list[tuple[float, float, float]]:
    """drop ticks older than keep_sec. the slow-move check needs about 2 minutes."""
    cutoff = ts - keep_sec
    return [row for row in samples if row[0] >= cutoff]


def _usd(n: float) -> str:
    """market cap for the agent. no raw price decimals."""
    return f"${n:,.0f}"


def first_alert_note(
    direction: str,
    change_pct: float,
    elapsed_sec: float,
    from_mcap: float,
    to_mcap: float,
) -> str:
    """said once per coin. later thinks see it in the history, so dont repeat it."""
    word = "increased" if direction == "up" else "decreased"
    return (
        f"market cap has {word} massively in a short space of time: "
        f"{change_pct:+.1f}% in {elapsed_sec:.1f}s "
        f"({_usd(from_mcap)} -> {_usd(to_mcap)}). "
        "you set the take-profit and stop-loss. "
        "reply revise_tpsl with a new profit_pct and loss_pct if you want different levels, "
        "or sell if you want out"
    )


def since_last_peek(
    prev_mcap: float | None,
    prev_ts: float | None,
    mcap: float,
    now: float,
) -> dict[str, Any]:
    """mcap change since the previous think. first look has nothing to diff."""
    if prev_mcap is None or prev_mcap <= 0 or prev_ts is None or mcap <= 0:
        return {
            "since_last_pct": None,
            "since_last_sec": None,
            "from_mcap_usd": None,
            "to_mcap_usd": mcap if mcap > 0 else None,
        }
    return {
        "since_last_pct": (mcap - prev_mcap) / prev_mcap * 100.0,
        "since_last_sec": max(0.0, now - prev_ts),
        "from_mcap_usd": prev_mcap,
        "to_mcap_usd": mcap,
    }


def since_last_line(info: dict[str, Any] | None) -> str:
    """one prompt line. none yet on the first peek."""
    if not info or info.get("since_last_pct") is None:
        return "since last peek: none yet"
    pct = float(info["since_last_pct"])
    sec = float(info["since_last_sec"])
    src = float(info["from_mcap_usd"])
    dst = float(info["to_mcap_usd"])
    return f"since last peek: {pct:+.2f}% in {sec:.0f}s ({_usd(src)} -> {_usd(dst)})"


def alert_line(payload: dict[str, Any]) -> str:
    """prompt/follow text. the long note only exists on the first alert."""
    note = str(payload.get("note") or "").strip()
    if note:
        return note
    pct = float(payload.get("change_pct") or 0)
    elapsed = float(payload.get("elapsed_sec") or 0)
    src = payload.get("from_mcap_usd")
    dst = payload.get("to_mcap_usd")
    if src is None:
        src = payload.get("from_fdv_usd") or 0
    if dst is None:
        dst = payload.get("to_fdv_usd") or 0
    return f"{pct:+.1f}% in {elapsed:.1f}s ({_usd(float(src))} -> {_usd(float(dst))})"
