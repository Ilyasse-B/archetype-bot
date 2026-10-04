"""
persistent FIFO migration backlog (passed filter candidates).

stored in trading_ledger.db — survives daemon restarts.
agent (later) pops oldest unconsumed row when free.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from archetype.ingest.models import MigrationEvent

log = logging.getLogger("archetype.backlog")


@dataclass
class BacklogItem:
    """one queued candidate."""

    id: int
    mint: str
    coin_info: dict[str, Any]
    timestamp: str
    consumed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mint": self.mint,
            "coin_info": self.coin_info,
            "timestamp": self.timestamp,
            "consumed": self.consumed,
        }


def ensure_backlog_table(ledger_path: Path) -> None:
    """idempotent — schema init already creates this, but belt+suspenders."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_backlog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                coin_info TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                consumed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_backlog_consumed "
            "ON migration_backlog(consumed, id)"
        )
        conn.commit()


def _coin_info_from_event(event: MigrationEvent) -> dict[str, Any]:
    """snapshot worth keeping for the agent later."""
    metrics = dict(event.filter_metrics or {})
    # drop huge nested blobs if any
    meta = event.metadata or metrics.get("metadata") or {}
    return {
        "mint": event.mint,
        "name": event.name,
        "symbol": event.symbol,
        "signature": event.signature,
        "pool": event.pool,
        "received_at": (
            event.received_at.isoformat()
            if isinstance(event.received_at, datetime)
            else str(event.received_at)
        ),
        "market_cap_usd": metrics.get("market_cap_usd"),
        "estimated_global_fees_sol": metrics.get("estimated_global_fees_sol"),
        # true when vault estimate is under the soft bar (still passed hard floor)
        "fees_check_needed": bool(metrics.get("fees_check_needed")),
        "fees_warn_below_sol": metrics.get("fees_warn_below_sol"),
        "pool_sol_at_migrate": metrics.get("pool_sol_at_migrate"),
        "socials_present": metrics.get("socials_present") or meta.get("socials_present"),
        "twitter": meta.get("twitter"),
        "telegram": meta.get("telegram"),
        "website": meta.get("website"),
        "image": meta.get("image"),
        "description": (meta.get("description") or "")[:500] or None,
        "deployer": metrics.get("deployer"),
        "pool_address": metrics.get("pool"),
    }


def enqueue_passed(
    ledger_path: Path,
    event: MigrationEvent,
    *,
    skip_if_pending: bool = True,
) -> BacklogItem | None:
    """
    append a PASS candidate to the backlog (FIFO by id).

    if skip_if_pending and same mint already waiting, skip duplicate.
    returns the new row, or None if skipped.
    """
    ensure_backlog_table(ledger_path)
    mint = event.mint
    info = _coin_info_from_event(event)

    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        if skip_if_pending:
            existing = conn.execute(
                "SELECT id FROM migration_backlog "
                "WHERE mint = ? AND consumed = 0 LIMIT 1",
                (mint,),
            ).fetchone()
            if existing:
                log.info(
                    "backlog skip duplicate pending mint=%s (id=%s)",
                    mint,
                    existing["id"],
                )
                return None

        cur = conn.execute(
            "INSERT INTO migration_backlog (mint, coin_info) VALUES (?, ?)",
            (mint, json.dumps(info)),
        )
        row_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute(
            "SELECT id, mint, coin_info, timestamp, consumed "
            "FROM migration_backlog WHERE id = ?",
            (row_id,),
        ).fetchone()

    item = _row_to_item(row)
    log.info(
        "BACKLOG +#%s mint=%s name=%s pending=%s",
        item.id,
        mint,
        (info.get("name") or "?")[:24],
        pending_count(ledger_path),
    )
    return item


def peek_fifo(ledger_path: Path) -> BacklogItem | None:
    """oldest unconsumed item (does not mark consumed)."""
    ensure_backlog_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, mint, coin_info, timestamp, consumed "
            "FROM migration_backlog WHERE consumed = 0 "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return _row_to_item(row) if row else None


def dequeue_fifo(ledger_path: Path) -> BacklogItem | None:
    """pop oldest unconsumed item (mark consumed=1)."""
    ensure_backlog_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, mint, coin_info, timestamp, consumed "
            "FROM migration_backlog WHERE consumed = 0 "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE migration_backlog SET consumed = 1 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
    item = _row_to_item(row)
    item.consumed = True
    log.info("BACKLOG pop #%s mint=%s", item.id, item.mint)
    return item


def consume_id(ledger_path: Path, backlog_id: int) -> BacklogItem | None:
    """
    mark a specific pending row consumed (first decision on that coin).

    returns the item if it was still pending, else None.
    """
    ensure_backlog_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, mint, coin_info, timestamp, consumed "
            "FROM migration_backlog WHERE id = ? AND consumed = 0",
            (backlog_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE migration_backlog SET consumed = 1 WHERE id = ?",
            (backlog_id,),
        )
        conn.commit()
    item = _row_to_item(row)
    item.consumed = True
    log.info("BACKLOG consume #%s mint=%s", item.id, item.mint)
    return item


def list_pending(ledger_path: Path, limit: int = 50) -> list[BacklogItem]:
    """pending items oldest → newest."""
    ensure_backlog_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, mint, coin_info, timestamp, consumed "
            "FROM migration_backlog WHERE consumed = 0 "
            "ORDER BY id ASC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def clear_pending(ledger_path: Path) -> int:
    """drop unconsumed coins left over from the last daemon run."""
    ensure_backlog_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        cur = conn.execute("DELETE FROM migration_backlog WHERE consumed = 0")
        conn.commit()
        return int(cur.rowcount)


def pending_count(ledger_path: Path) -> int:
    """how many waiting."""
    ensure_backlog_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM migration_backlog WHERE consumed = 0"
        ).fetchone()
    return int(row[0] if row else 0)


def _row_to_item(row: sqlite3.Row) -> BacklogItem:
    raw = row["coin_info"]
    try:
        info = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except json.JSONDecodeError:
        info = {"raw": raw}
    return BacklogItem(
        id=int(row["id"]),
        mint=str(row["mint"]),
        coin_info=info,
        timestamp=str(row["timestamp"]),
        consumed=bool(row["consumed"]),
    )
