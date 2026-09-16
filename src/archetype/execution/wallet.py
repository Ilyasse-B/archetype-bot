"""
sqlite-backed paper wallet + open positions (trading_ledger.db).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_START_SOL = 100.0


@dataclass
class WalletState:
    """singleton cash balance."""

    sol_balance: float


@dataclass
class Position:
    """one open paper bag."""

    id: int
    mint: str
    name: str | None
    symbol: str | None
    tokens: float
    cost_sol: float
    entry_price_sol: float
    entry_fdv_usd: float | None
    opened_at: str
    # first-buy size; used by `sell init` (not bumped on rebuy)
    initial_cost_sol: float = 0.0
    # pumpswap pool address (helps skip holder-scan resolve)
    pool: str | None = None

    @property
    def label(self) -> str:
        """ticker or short mint for display."""
        return self.symbol or self.name or self.mint[:8]


def ensure_wallet(ledger_path: Path, *, start_sol: float = DEFAULT_START_SOL) -> WalletState:
    """create tables + singleton row if missing."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_wallet (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                sol_balance REAL NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL UNIQUE,
                name TEXT,
                symbol TEXT,
                tokens REAL NOT NULL,
                cost_sol REAL NOT NULL,
                entry_price_sol REAL NOT NULL,
                entry_fdv_usd REAL,
                initial_cost_sol REAL NOT NULL DEFAULT 0,
                pool TEXT,
                opened_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        _migrate_positions(conn)
        row = conn.execute(
            "SELECT sol_balance FROM paper_wallet WHERE id = 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO paper_wallet (id, sol_balance) VALUES (1, ?)",
                (float(start_sol),),
            )
            conn.commit()
            return WalletState(sol_balance=float(start_sol))
        conn.commit()
        return WalletState(sol_balance=float(row[0]))


def _migrate_positions(conn: sqlite3.Connection) -> None:
    """add columns for dbs created before them."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_positions)")}
    if "initial_cost_sol" not in cols:
        conn.execute(
            "ALTER TABLE paper_positions ADD COLUMN initial_cost_sol REAL"
        )
        # best-effort: treat current cost as the initial for old rows
        conn.execute(
            "UPDATE paper_positions SET initial_cost_sol = cost_sol "
            "WHERE initial_cost_sol IS NULL"
        )
    if "pool" not in cols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN pool TEXT")


def get_balance(ledger_path: Path) -> float:
    """current mock SOL cash."""
    return ensure_wallet(ledger_path).sol_balance


def set_balance(ledger_path: Path, sol: float) -> float:
    """overwrite cash (used internally after fills)."""
    ensure_wallet(ledger_path)
    if sol < 0:
        raise ValueError("sol balance cannot be negative")
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            "UPDATE paper_wallet SET sol_balance = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
            (float(sol),),
        )
        conn.commit()
    return float(sol)


def add_sol(ledger_path: Path, amount: float) -> float:
    """top up mock SOL; returns new balance."""
    if amount <= 0:
        raise ValueError("amount must be > 0")
    bal = get_balance(ledger_path)
    return set_balance(ledger_path, bal + float(amount))


def reset_wallet(
    ledger_path: Path, *, start_sol: float = DEFAULT_START_SOL
) -> float:
    """wipe open positions + set cash back to start_sol."""
    ensure_wallet(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("DELETE FROM paper_positions")
        conn.execute(
            "UPDATE paper_wallet SET sol_balance = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
            (float(start_sol),),
        )
        conn.commit()
    return float(start_sol)


_POS_COLS = (
    "id, mint, name, symbol, tokens, cost_sol, "
    "entry_price_sol, entry_fdv_usd, initial_cost_sol, pool, opened_at"
)


def list_positions(ledger_path: Path) -> list[Position]:
    """all open positions."""
    ensure_wallet(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT {_POS_COLS} FROM paper_positions ORDER BY id ASC"
        ).fetchall()
    return [_row_to_pos(r) for r in rows]


def get_position(ledger_path: Path, mint: str) -> Position | None:
    """open position for mint, if any."""
    ensure_wallet(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {_POS_COLS} FROM paper_positions WHERE mint = ?",
            (mint,),
        ).fetchone()
    return _row_to_pos(row) if row else None


def upsert_position(
    ledger_path: Path,
    *,
    mint: str,
    name: str | None,
    symbol: str | None,
    tokens: float,
    cost_sol: float,
    entry_price_sol: float,
    entry_fdv_usd: float | None,
    initial_cost_sol: float,
    pool: str | None = None,
) -> Position:
    """insert or replace open position for mint."""
    ensure_wallet(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT id, pool FROM paper_positions WHERE mint = ?", (mint,)
        ).fetchone()
        pool_val = pool or (existing["pool"] if existing else None)
        if existing:
            conn.execute(
                """
                UPDATE paper_positions SET
                    name = ?, symbol = ?, tokens = ?, cost_sol = ?,
                    entry_price_sol = ?, entry_fdv_usd = ?,
                    initial_cost_sol = ?, pool = ?
                WHERE mint = ?
                """,
                (
                    name,
                    symbol,
                    float(tokens),
                    float(cost_sol),
                    float(entry_price_sol),
                    entry_fdv_usd,
                    float(initial_cost_sol),
                    pool_val,
                    mint,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO paper_positions
                    (mint, name, symbol, tokens, cost_sol,
                     entry_price_sol, entry_fdv_usd, initial_cost_sol, pool)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mint,
                    name,
                    symbol,
                    float(tokens),
                    float(cost_sol),
                    float(entry_price_sol),
                    entry_fdv_usd,
                    float(initial_cost_sol),
                    pool_val,
                ),
            )
        conn.commit()
        row = conn.execute(
            f"SELECT {_POS_COLS} FROM paper_positions WHERE mint = ?",
            (mint,),
        ).fetchone()
    assert row is not None
    return _row_to_pos(row)


def delete_position(ledger_path: Path, mint: str) -> None:
    """drop open position row."""
    ensure_wallet(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("DELETE FROM paper_positions WHERE mint = ?", (mint,))
        conn.commit()


def record_closed_trade(
    ledger_path: Path,
    *,
    mint: str,
    entry_price: float,
    exit_price: float,
    size_sol: float,
    pnl_sol: float,
    opened_at: str | None,
    status: str = "closed",
) -> None:
    """append a closed/partial row to trades (history)."""
    ensure_wallet(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        # trades table may already exist from schema init
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                size_sol REAL,
                tp REAL,
                sl REAL,
                pnl_sol REAL,
                thesis TEXT,
                time_held_seconds INTEGER,
                opened_at DATETIME,
                closed_at DATETIME,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trades
                (mint, status, entry_price, exit_price, size_sol,
                 pnl_sol, opened_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                mint,
                status,
                float(entry_price),
                float(exit_price),
                float(size_sol),
                float(pnl_sol),
                opened_at,
            ),
        )
        conn.commit()


def _row_to_pos(row: sqlite3.Row) -> Position:
    keys = row.keys()
    initial = row["initial_cost_sol"] if "initial_cost_sol" in keys else None
    if initial is None:
        initial = row["cost_sol"]
    pool = row["pool"] if "pool" in keys else None
    return Position(
        id=int(row["id"]),
        mint=str(row["mint"]),
        name=row["name"],
        symbol=row["symbol"],
        tokens=float(row["tokens"]),
        cost_sol=float(row["cost_sol"]),
        entry_price_sol=float(row["entry_price_sol"]),
        entry_fdv_usd=(
            float(row["entry_fdv_usd"])
            if row["entry_fdv_usd"] is not None
            else None
        ),
        opened_at=str(row["opened_at"]),
        initial_cost_sol=float(initial),
        pool=str(pool) if pool else None,
    )
