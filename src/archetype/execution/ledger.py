"""
trade ledger — atomic fills + finalized rounds.

trade_fills: every buy/sell leg
trade_rounds: one row per coin lifecycle (open → closed on flat)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def ensure_trade_ledger(ledger_path: Path) -> None:
    """create fills + rounds tables (idempotent)."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trade_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                name TEXT,
                symbol TEXT,
                pool TEXT,
                entry_price_sol REAL,
                exit_price_sol REAL,
                entry_fdv_usd REAL,
                exit_fdv_usd REAL,
                bought_sol REAL NOT NULL DEFAULT 0,
                sold_sol REAL NOT NULL DEFAULT 0,
                pnl_sol REAL,
                pnl_pct REAL,
                buy_count INTEGER NOT NULL DEFAULT 0,
                sell_count INTEGER NOT NULL DEFAULT 0,
                time_held_seconds INTEGER,
                opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME,
                thesis TEXT,
                tp REAL,
                sl REAL
            );
            CREATE INDEX IF NOT EXISTS idx_rounds_mint
                ON trade_rounds(mint, status);

            CREATE TABLE IF NOT EXISTS trade_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                side TEXT NOT NULL,
                mode TEXT,
                sol REAL NOT NULL,
                tokens REAL NOT NULL,
                price_sol REAL NOT NULL,
                fdv_usd REAL,
                cost_basis_sol REAL,
                pnl_sol REAL,
                name TEXT,
                symbol TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (round_id) REFERENCES trade_rounds(id)
            );
            CREATE INDEX IF NOT EXISTS idx_fills_round
                ON trade_fills(round_id, id);
            CREATE INDEX IF NOT EXISTS idx_fills_mint
                ON trade_fills(mint, id);
            """
        )
        conn.commit()


def open_round(
    ledger_path: Path,
    *,
    mint: str,
    name: str | None = None,
    symbol: str | None = None,
    pool: str | None = None,
    entry_price_sol: float | None = None,
    entry_fdv_usd: float | None = None,
) -> int:
    """start a new open round; returns round id."""
    ensure_trade_ledger(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO trade_rounds
                (mint, status, name, symbol, pool,
                 entry_price_sol, entry_fdv_usd)
            VALUES (?, 'open', ?, ?, ?, ?, ?)
            """,
            (mint, name, symbol, pool, entry_price_sol, entry_fdv_usd),
        )
        conn.commit()
        return int(cur.lastrowid)


def record_fill(
    ledger_path: Path,
    *,
    round_id: int,
    mint: str,
    side: str,
    sol: float,
    tokens: float,
    price_sol: float,
    fdv_usd: float | None = None,
    mode: str | None = None,
    cost_basis_sol: float | None = None,
    pnl_sol: float | None = None,
    name: str | None = None,
    symbol: str | None = None,
) -> int:
    """append one buy/sell leg; returns fill id."""
    ensure_trade_ledger(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO trade_fills
                (round_id, mint, side, mode, sol, tokens, price_sol,
                 fdv_usd, cost_basis_sol, pnl_sol, name, symbol)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(round_id),
                mint,
                side,
                mode,
                float(sol),
                float(tokens),
                float(price_sol),
                fdv_usd,
                cost_basis_sol,
                pnl_sol,
                name,
                symbol,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


@dataclass
class ClosedRound:
    """summary after finalize."""

    id: int
    mint: str
    bought_sol: float
    sold_sol: float
    pnl_sol: float
    pnl_pct: float
    buy_count: int
    sell_count: int
    time_held_seconds: int | None


def finalize_round(
    ledger_path: Path,
    round_id: int,
    *,
    exit_price_sol: float | None = None,
    exit_fdv_usd: float | None = None,
    name: str | None = None,
    symbol: str | None = None,
) -> ClosedRound:
    """
    close an open round — aggregate all fills into the round row.

    realized pnl = total sold SOL − total bought SOL.
    """
    ensure_trade_ledger(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        fills = conn.execute(
            "SELECT side, sol, price_sol, fdv_usd, timestamp "
            "FROM trade_fills WHERE round_id = ? ORDER BY id ASC",
            (int(round_id),),
        ).fetchall()
        round_row = conn.execute(
            "SELECT * FROM trade_rounds WHERE id = ?", (int(round_id),)
        ).fetchone()
        if round_row is None:
            raise ValueError(f"round {round_id} not found")

        bought = 0.0
        sold = 0.0
        buy_n = 0
        sell_n = 0
        first_buy_price = None
        first_buy_fdv = None
        last_sell_price = exit_price_sol
        last_sell_fdv = exit_fdv_usd

        for f in fills:
            if f["side"] == "buy":
                bought += float(f["sol"])
                buy_n += 1
                if first_buy_price is None:
                    first_buy_price = float(f["price_sol"])
                    first_buy_fdv = (
                        float(f["fdv_usd"]) if f["fdv_usd"] is not None else None
                    )
            elif f["side"] == "sell":
                sold += float(f["sol"])
                sell_n += 1
                last_sell_price = float(f["price_sol"])
                if f["fdv_usd"] is not None:
                    last_sell_fdv = float(f["fdv_usd"])

        pnl = sold - bought
        pnl_pct = (pnl / bought * 100.0) if bought > 0 else 0.0

        opened_at = round_row["opened_at"]
        held = None
        if opened_at:
            try:
                # sqlite current_timestamp is UTC-ish naive
                start = datetime.fromisoformat(str(opened_at).replace("Z", ""))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                held = int(
                    (datetime.now(timezone.utc) - start).total_seconds()
                )
            except ValueError:
                held = None

        entry_price = round_row["entry_price_sol"] or first_buy_price
        entry_fdv = round_row["entry_fdv_usd"] or first_buy_fdv

        conn.execute(
            """
            UPDATE trade_rounds SET
                status = 'closed',
                name = COALESCE(?, name),
                symbol = COALESCE(?, symbol),
                entry_price_sol = ?,
                exit_price_sol = ?,
                entry_fdv_usd = ?,
                exit_fdv_usd = ?,
                bought_sol = ?,
                sold_sol = ?,
                pnl_sol = ?,
                pnl_pct = ?,
                buy_count = ?,
                sell_count = ?,
                time_held_seconds = ?,
                closed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                name,
                symbol,
                entry_price,
                last_sell_price,
                entry_fdv,
                last_sell_fdv,
                bought,
                sold,
                pnl,
                pnl_pct,
                buy_n,
                sell_n,
                held,
                int(round_id),
            ),
        )
        conn.commit()

    return ClosedRound(
        id=int(round_id),
        mint=str(round_row["mint"]),
        bought_sol=bought,
        sold_sol=sold,
        pnl_sol=pnl,
        pnl_pct=pnl_pct,
        buy_count=buy_n,
        sell_count=sell_n,
        time_held_seconds=held,
    )


def list_fills_for_round(ledger_path: Path, round_id: int) -> list[dict[str, Any]]:
    """fills oldest → newest for a round."""
    ensure_trade_ledger(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trade_fills WHERE round_id = ? ORDER BY id ASC",
            (int(round_id),),
        ).fetchall()
    return [dict(r) for r in rows]
