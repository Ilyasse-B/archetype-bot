"""
memory for the coin the agent is on.

mid-trade prompt: rolling summary + last 5 events. never the full log.
on close: full history is for the thesis call only, then we write
trade_rounds.thesis. pnl and hold time are already on that row.
after that, drop the sliding copy.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# how many events a think step is allowed to see
PROMPT_EVENT_LIMIT = 5
# leftover events for coins we already left
STALE_DAYS = 5


@dataclass
class FocusEvent:
    """one thing that happened while this mint was the focus."""

    id: int
    kind: str
    payload: dict[str, Any]
    timestamp: str


@dataclass
class PromptWindow:
    """the only slice a mid-trade think step should read."""

    mint: str
    summary: str
    events: list[FocusEvent]


@dataclass
class RoundThesis:
    """closed round after the final thesis is stored."""

    id: int
    mint: str
    thesis: str
    pnl_sol: float | None
    pnl_pct: float | None
    time_held_seconds: int | None
    opened_at: str | None
    closed_at: str | None


class FocusMemory:
    """events + summary in active_context.db; thesis lands on the ledger."""

    def __init__(
        self,
        active_path: Path,
        ledger_path: Path,
        *,
        prompt_events: int = PROMPT_EVENT_LIMIT,
    ) -> None:
        self.active_path = active_path
        self.ledger_path = ledger_path
        self.prompt_events = max(1, int(prompt_events))
        self._ensure()

    def begin_focus(self, mint: str) -> None:
        """fresh pass on this mint — drop any old events/summary for it."""
        self._clear(mint.strip())

    def drop_focus(self, mint: str) -> None:
        """thesis is on the round now, so the sliding copy can go."""
        self._clear(mint.strip())

    def save_round_log(self, round_id: int, mint: str) -> None:
        """stick the full step log on the closed trade, for training later."""
        blob = json.dumps(self._log_blob(mint))
        with sqlite3.connect(self.ledger_path) as conn:
            _add_column(conn, "trade_rounds", "agent_log", "TEXT")
            conn.execute(
                "UPDATE trade_rounds SET agent_log = ? WHERE id = ?",
                (blob, int(round_id)),
            )
            conn.commit()

    def save_reject_log(self, mint: str) -> None:
        """same log, on the latest reject row for this mint."""
        blob = json.dumps(self._log_blob(mint))
        with sqlite3.connect(self.ledger_path) as conn:
            _add_column(conn, "agent_rejects", "agent_log", "TEXT")
            conn.execute(
                """
                UPDATE agent_rejects SET agent_log = ?
                WHERE id = (
                    SELECT id FROM agent_rejects
                    WHERE mint = ?
                    ORDER BY id DESC
                    LIMIT 1
                )
                """,
                (blob, mint.strip()),
            )
            conn.commit()

    def append_event(
        self, mint: str, kind: str, payload: dict[str, Any]
    ) -> FocusEvent:
        """append one event. does not show up in the prompt beyond the last 5."""
        mint = mint.strip()
        kind = kind.strip()
        if not mint or not kind:
            raise ValueError("mint and kind are required")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        self._prune_others(mint)
        with sqlite3.connect(self.active_path) as conn:
            cur = conn.execute(
                "INSERT INTO active_context (source, entity_key, raw_payload) "
                "VALUES (?, ?, ?)",
                (kind, mint, json.dumps(payload)),
            )
            conn.commit()
            row_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT id, source, raw_payload, timestamp "
                "FROM active_context WHERE id = ?",
                (row_id,),
            ).fetchone()
        return _event_from_row(row)

    def set_summary(self, mint: str, summary: str) -> None:
        """replace the rolling summary. this is what the next prompt leads with."""
        mint = mint.strip()
        text = summary.strip()
        if not mint:
            raise ValueError("mint is required")
        if not text:
            raise ValueError("summary is empty")
        with sqlite3.connect(self.active_path) as conn:
            conn.execute(
                """
                INSERT INTO focus_summary (mint, summary, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(mint) DO UPDATE SET
                    summary = excluded.summary,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (mint, text),
            )
            conn.commit()

    def prompt_window(self, mint: str) -> PromptWindow:
        """summary + last N events, oldest first. not the full history."""
        mint = mint.strip()
        summary = self._summary(mint)
        with sqlite3.connect(self.active_path) as conn:
            rows = conn.execute(
                """
                SELECT id, source, raw_payload, timestamp
                FROM active_context
                WHERE entity_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (mint, self.prompt_events),
            ).fetchall()
        events = [_event_from_row(r) for r in reversed(rows)]
        return PromptWindow(mint=mint, summary=summary, events=events)

    def history_for_thesis(self, mint: str) -> list[FocusEvent]:
        """
        every event for this mint, oldest first.

        only the close/thesis step should call this.
        """
        mint = mint.strip()
        with sqlite3.connect(self.active_path) as conn:
            rows = conn.execute(
                """
                SELECT id, source, raw_payload, timestamp
                FROM active_context
                WHERE entity_key = ?
                ORDER BY id ASC
                """,
                (mint,),
            ).fetchall()
        return [_event_from_row(r) for r in rows]

    def round_row(self, round_id: int) -> dict[str, Any] | None:
        """the trade row, so the close prompt can see pnl and the coin name."""
        with sqlite3.connect(self.ledger_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trade_rounds WHERE id = ?",
                (int(round_id),),
            ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def write_thesis(self, round_id: int, thesis: str) -> RoundThesis:
        """
        store the final thesis on a closed trade_rounds row.

        pnl and time_held_seconds are already filled when the round closed.
        """
        text = thesis.strip()
        if not text:
            raise ValueError("thesis is empty")
        with sqlite3.connect(self.ledger_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trade_rounds WHERE id = ?",
                (int(round_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"round {round_id} not found")
            if row["status"] != "closed":
                raise ValueError(
                    "round is still open — write the thesis after it closes"
                )
            conn.execute(
                "UPDATE trade_rounds SET thesis = ? WHERE id = ?",
                (text, int(round_id)),
            )
            conn.commit()
            saved = conn.execute(
                "SELECT * FROM trade_rounds WHERE id = ?",
                (int(round_id),),
            ).fetchone()
        return _thesis_from_row(saved, text)

    def _ensure(self) -> None:
        """tables the daemon schema also creates — ok if the db already exists."""
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.active_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS active_context (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    entity_key TEXT,
                    raw_payload TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_context_timestamp
                    ON active_context(timestamp);
                CREATE INDEX IF NOT EXISTS idx_context_entity
                    ON active_context(entity_key);
                CREATE TABLE IF NOT EXISTS focus_summary (
                    mint TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()

    def _log_blob(self, mint: str) -> dict[str, Any]:
        """summary plus every event. this is what we keep after the coin is gone."""
        return {
            "summary": self._summary(mint),
            "events": [
                {
                    "id": ev.id,
                    "kind": ev.kind,
                    "payload": ev.payload,
                    "timestamp": ev.timestamp,
                }
                for ev in self.history_for_thesis(mint)
            ],
        }

    def _summary(self, mint: str) -> str:
        with sqlite3.connect(self.active_path) as conn:
            row = conn.execute(
                "SELECT summary FROM focus_summary WHERE mint = ?",
                (mint,),
            ).fetchone()
        if row is None:
            return ""
        return str(row[0])

    def _clear(self, mint: str) -> None:
        with sqlite3.connect(self.active_path) as conn:
            conn.execute(
                "DELETE FROM active_context WHERE entity_key = ?", (mint,)
            )
            conn.execute("DELETE FROM focus_summary WHERE mint = ?", (mint,))
            conn.commit()

    def _prune_others(self, keep_mint: str) -> None:
        """drop other coins' events once they're older than the window."""
        with sqlite3.connect(self.active_path) as conn:
            conn.execute(
                """
                DELETE FROM active_context
                WHERE entity_key != ?
                  AND timestamp < datetime('now', ?)
                """,
                (keep_mint, f"-{STALE_DAYS} days"),
            )
            conn.execute(
                """
                DELETE FROM focus_summary
                WHERE mint != ?
                  AND updated_at < datetime('now', ?)
                """,
                (keep_mint, f"-{STALE_DAYS} days"),
            )
            conn.commit()


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """old db files were created before this column existed."""
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _event_from_row(row: tuple[Any, ...] | sqlite3.Row) -> FocusEvent:
    raw = row[2]
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return FocusEvent(
        id=int(row[0]),
        kind=str(row[1]),
        payload=payload,
        timestamp=str(row[3] or ""),
    )


def _thesis_from_row(row: sqlite3.Row, thesis: str) -> RoundThesis:
    def _f(key: str) -> float | None:
        val = row[key]
        return float(val) if val is not None else None

    held = row["time_held_seconds"]
    return RoundThesis(
        id=int(row["id"]),
        mint=str(row["mint"]),
        thesis=thesis,
        pnl_sol=_f("pnl_sol"),
        pnl_pct=_f("pnl_pct"),
        time_held_seconds=int(held) if held is not None else None,
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
    )
