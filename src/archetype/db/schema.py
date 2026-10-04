"""
sqlite schema stubs for phase 1.

two dbs:
- active_context.db  → short-lived events
- trading_ledger.db  → permanent rejects / trades / backlog / training rows
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# --- active context (sliding window later) ---
ACTIVE_CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    entity_key TEXT,
    raw_payload TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_context_timestamp ON active_context(timestamp);
CREATE INDEX IF NOT EXISTS idx_context_entity ON active_context(entity_key);

-- one rolling summary per focused mint (prompt sees this + last 5 events)
CREATE TABLE IF NOT EXISTS focus_summary (
    mint TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

# --- permanent ledger ---
TRADING_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS filter_rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    reason TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    decision TEXT NOT NULL,
    agent_log TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- coins reviewed but not rejected: left aside, or traded
CREATE TABLE IF NOT EXISTS agent_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    backlog_id INTEGER,
    outcome TEXT NOT NULL,
    coin_info TEXT,
    notes TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reviews_mint ON agent_reviews(mint);
CREATE INDEX IF NOT EXISTS idx_reviews_outcome ON agent_reviews(outcome);

CREATE TABLE IF NOT EXISTS migration_backlog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    coin_info TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    consumed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_backlog_consumed ON migration_backlog(consumed, id);

-- legacy stub (no longer written; kept for old dbs)
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
);

-- atomic buy/sell legs
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
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fills_round ON trade_fills(round_id, id);
CREATE INDEX IF NOT EXISTS idx_fills_mint ON trade_fills(mint, id);

-- one lifecycle per coin bag (open → closed when flat)
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
    agent_log TEXT,
    tp REAL,
    sl REAL
);

CREATE INDEX IF NOT EXISTS idx_rounds_mint ON trade_rounds(mint, status);

-- paper wallet (singleton row id=1)
CREATE TABLE IF NOT EXISTS paper_wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sol_balance REAL NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- open paper positions (one row per mint while open)
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
    bought_sol REAL NOT NULL DEFAULT 0,
    sold_sol REAL NOT NULL DEFAULT 0,
    round_id INTEGER,
    opened_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pool_cache (
    mint TEXT PRIMARY KEY,
    pool TEXT NOT NULL,
    base_ata TEXT NOT NULL,
    quote_ata TEXT NOT NULL,
    decimals INTEGER NOT NULL,
    supply_ui REAL NOT NULL,
    name TEXT,
    symbol TEXT,
    creator TEXT NOT NULL,
    base_mint TEXT NOT NULL,
    quote_mint TEXT NOT NULL,
    lp_mint TEXT NOT NULL,
    lp_supply INTEGER NOT NULL,
    coin_creator TEXT,
    pool_index INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist (
    mint TEXT PRIMARY KEY,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blacklist_deployers (
    deployer TEXT PRIMARY KEY,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def init_databases(active_path: Path, ledger_path: Path) -> None:
    """
    create data dir + both sqlite files with stub tables.

    sync sqlite is fine for phase 1 bootstrap.
    """
    active_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(active_path) as conn:
        conn.executescript(ACTIVE_CONTEXT_SCHEMA)
        conn.commit()

    with sqlite3.connect(ledger_path) as conn:
        conn.executescript(TRADING_LEDGER_SCHEMA)
        conn.commit()


def db_exists_and_ok(path: Path) -> bool:
    """quick check that the file exists and opens."""
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path) as conn:
            conn.execute("SELECT 1")
        return True
    except sqlite3.Error:
        return False
