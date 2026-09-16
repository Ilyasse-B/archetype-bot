"""
agent_rejects + agent_reviews in trading_ledger.db.

rejects = tossed forever (for now).
reviews = left aside (exited) or traded — history without calling it a reject.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def ensure_agent_tables(ledger_path: Path) -> None:
    """idempotent create for rejects + reviews."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_rejects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                decision TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                backlog_id INTEGER,
                outcome TEXT NOT NULL,
                coin_info TEXT,
                notes TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_mint ON agent_reviews(mint)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_outcome "
            "ON agent_reviews(outcome)"
        )
        conn.commit()


def record_reject(
    ledger_path: Path,
    mint: str,
    *,
    reasoning: str = "manual reject (cli trade stream)",
    decision: str = "reject",
) -> int:
    """append to agent_rejects; returns row id."""
    ensure_agent_tables(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        cur = conn.execute(
            "INSERT INTO agent_rejects (mint, reasoning, decision) "
            "VALUES (?, ?, ?)",
            (mint, reasoning, decision),
        )
        conn.commit()
        return int(cur.lastrowid)


def record_review(
    ledger_path: Path,
    mint: str,
    outcome: str,
    *,
    backlog_id: int | None = None,
    coin_info: dict[str, Any] | None = None,
    notes: str | None = None,
) -> int:
    """
    append to agent_reviews.

    outcome: 'exited' (watched then left) or 'traded' (bought at least once).
    """
    ensure_agent_tables(ledger_path)
    info_s = json.dumps(coin_info) if coin_info is not None else None
    with sqlite3.connect(ledger_path) as conn:
        cur = conn.execute(
            "INSERT INTO agent_reviews "
            "(mint, backlog_id, outcome, coin_info, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (mint, backlog_id, outcome, info_s, notes),
        )
        conn.commit()
        return int(cur.lastrowid)
