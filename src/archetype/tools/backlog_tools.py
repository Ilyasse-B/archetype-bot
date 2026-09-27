"""
backlog tools — peek without consume + FIFO next for search mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archetype.backlog import dequeue_fifo, list_pending, pending_count
from archetype.tools.models import BacklogPeekItem


def peek_backlog(ledger_path: Path, *, limit: int = 10) -> dict[str, Any]:
    """list pending candidates without consuming."""
    items = list_pending(ledger_path, limit=limit)
    out: list[BacklogPeekItem] = []
    for it in items:
        info = it.coin_info or {}
        out.append(
            BacklogPeekItem(
                id=it.id,
                mint=it.mint,
                timestamp=it.timestamp,
                name=info.get("name"),
                symbol=info.get("symbol"),
                market_cap_usd=_f(info.get("market_cap_usd")),
                estimated_global_fees_sol=_f(
                    info.get("estimated_global_fees_sol")
                ),
                fees_check_needed=bool(info.get("fees_check_needed")),
                socials_present=list(info.get("socials_present") or []),
                coin_info=info,
            )
        )
    return {
        "ok": True,
        "pending": pending_count(ledger_path),
        "items": [i.model_dump() for i in out],
    }


def next_candidate(ledger_path: Path) -> dict[str, Any]:
    """pop oldest backlog row (consumed) for FREE/searching agent."""
    item = dequeue_fifo(ledger_path)
    if item is None:
        return {"ok": True, "item": None, "message": "backlog empty"}
    return {"ok": True, "item": item.to_dict()}


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
