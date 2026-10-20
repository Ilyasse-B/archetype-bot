"""
price change for the coin in focus.

every think says how market cap moved since the last peek.
any size, any speed. the first look has nothing to diff.
"""

from __future__ import annotations

from typing import Any


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


def _usd(n: float) -> str:
    """market cap for the agent. no raw price decimals."""
    return f"${n:,.0f}"
