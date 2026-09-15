"""
blacklist lookups against trading_ledger.db (mint + deployer).

empty by default — we just honour rows if you add them later.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def ensure_blacklist_tables(ledger_path: Path) -> None:
    """make sure blacklist tables exist (deployer table is new)."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                mint TEXT PRIMARY KEY,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist_deployers (
                deployer TEXT PRIMARY KEY,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def is_mint_blacklisted(ledger_path: Path, mint: str) -> tuple[bool, str | None]:
    """check mint blacklist."""
    if not ledger_path.is_file():
        return False, None
    with sqlite3.connect(ledger_path) as conn:
        row = conn.execute(
            "SELECT reason FROM blacklist WHERE mint = ?", (mint,)
        ).fetchone()
    if row:
        return True, row[0]
    return False, None


def is_deployer_blacklisted(ledger_path: Path, deployer: str) -> tuple[bool, str | None]:
    """check deployer blacklist."""
    if not ledger_path.is_file():
        return False, None
    with sqlite3.connect(ledger_path) as conn:
        row = conn.execute(
            "SELECT reason FROM blacklist_deployers WHERE deployer = ?",
            (deployer,),
        ).fetchone()
    if row:
        return True, row[0]
    return False, None


def log_filter_reject(ledger_path: Path, mint: str, reason: str) -> None:
    """append a filter reject row."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            "INSERT INTO filter_rejects (mint, reason) VALUES (?, ?)",
            (mint, reason),
        )
        conn.commit()
