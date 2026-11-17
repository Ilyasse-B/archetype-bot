"""
permanent local cache for urlscans legitimacy lookups.

keyed by normalized final url so a news page shared across coins
doesnt burn another free-tier scan.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def ensure_urlscans_cache_table(ledger_path: Path) -> None:
    """idempotent table create on the trading ledger."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS urlscans_cache (
                url_key TEXT PRIMARY KEY,
                verdict TEXT,
                score INTEGER,
                confidence INTEGER,
                reasons TEXT,
                raw_json TEXT,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


def urlscans_cache_get(
    url_key: str,
    *,
    ledger_path: Path | None,
) -> dict[str, Any] | None:
    """return cached urlScansAnalysis-shaped dict, or None."""
    key = (url_key or "").strip()
    if not key or ledger_path is None:
        return None
    ensure_urlscans_cache_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT verdict, score, confidence, reasons, raw_json "
            "FROM urlscans_cache WHERE url_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    reasons: Any = None
    raw_reasons = row["reasons"]
    if isinstance(raw_reasons, str) and raw_reasons.strip():
        try:
            reasons = json.loads(raw_reasons)
        except json.JSONDecodeError:
            reasons = raw_reasons
    out: dict[str, Any] = {
        "verdict": row["verdict"],
        "score": row["score"],
        "confidence": row["confidence"],
        "reasons": reasons,
    }
    raw = row["raw_json"]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # prefer full cached analysis if we stored it
                for k, v in parsed.items():
                    if k not in out or out[k] is None:
                        out[k] = v
        except json.JSONDecodeError:
            pass
    return out


def urlscans_cache_put(
    url_key: str,
    analysis: dict[str, Any],
    *,
    ledger_path: Path | None,
) -> None:
    """persist one urlscans analysis forever (no ttl)."""
    key = (url_key or "").strip()
    if not key or ledger_path is None or not isinstance(analysis, dict):
        return
    ensure_urlscans_cache_table(ledger_path)
    reasons = analysis.get("reasons")
    reasons_s = json.dumps(reasons) if reasons is not None else None
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            """
            INSERT INTO urlscans_cache (
                url_key, verdict, score, confidence, reasons, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_key) DO UPDATE SET
                verdict=excluded.verdict,
                score=excluded.score,
                confidence=excluded.confidence,
                reasons=excluded.reasons,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                key,
                analysis.get("verdict"),
                analysis.get("score"),
                analysis.get("confidence"),
                reasons_s,
                json.dumps(analysis),
                time.time(),
            ),
        )
        conn.commit()
