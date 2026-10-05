"""
per-coin chat log.

each think is saved as it happens. when focus ends we append the whole
session to transcripts/<mint>.txt so a test run can be read later.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("archetype.transcript")

# src/archetype/llm/transcript.py -> repo root
TRANSCRIPTS_DIR = Path(__file__).resolve().parents[3] / "transcripts"


def render_exchange(
    messages: list[dict[str, str]],
    reply: str | None,
    *,
    error: str | None = None,
) -> str:
    """one model call, roles in order, then whatever came back."""
    chunks: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        chunks.append(f"--- {role} ---\n{msg.get('content') or ''}")
    if reply is not None and reply.strip():
        chunks.append(f"--- assistant ---\n{reply.strip()}")
    elif error:
        chunks.append(f"--- assistant ---\n(no reply) {error}")
    else:
        chunks.append("--- assistant ---\n(no reply)")
    return "\n\n".join(chunks)


def render_think(
    n: int,
    messages: list[dict[str, str]],
    reply: str | None,
    *,
    error: str | None = None,
) -> str:
    """think number plus the exact messages that went out."""
    return f"----- think {n} -----\n\n{render_exchange(messages, reply, error=error)}"


def render_thesis(
    messages: list[dict[str, str]],
    reply: str | None,
    *,
    error: str | None = None,
) -> str:
    """the close call. same shape as a think."""
    return f"----- thesis -----\n\n{render_exchange(messages, reply, error=error)}"


def _safe_name(mint: str) -> str:
    """mint is base58. still strip anything that could leave the folder."""
    cleaned = "".join(ch for ch in mint.strip() if ch.isalnum())
    return cleaned or "unknown"


def append_session(
    mint: str,
    symbol: str | None,
    reason: str,
    body: str,
    *,
    directory: Path | None = None,
) -> Path | None:
    """
    append one finished focus to transcripts/<mint>.txt.

    a later pass on the same mint goes under the old one. empty body is skipped.
    """
    text = body.strip()
    if not mint.strip() or not text:
        return None
    folder = directory or TRANSCRIPTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe_name(mint)}.txt"
    label = (symbol or "").strip() or mint.strip()
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = (
        f"{'=' * 72}\n"
        f"{label}\n"
        f"mint: {mint.strip()}\n"
        f"ended: {when}\n"
        f"why: {reason}\n"
        f"{'=' * 72}\n\n"
    )
    with path.open("a", encoding="utf-8") as handle:
        if path.stat().st_size > 0:
            handle.write("\n")
        handle.write(header)
        handle.write(text)
        handle.write("\n")
    log.info("TRANSCRIPT %s %s", reason, path)
    return path


def flush_transcript(
    memory: Any,
    mint: str,
    symbol: str | None,
    reason: str,
    *,
    directory: Path | None = None,
) -> Path | None:
    """write the saved turns, if this coin had any."""
    if not mint:
        return None
    try:
        blocks = memory.turns(mint)
    except Exception:  # noqa: BLE001
        log.exception("transcript read failed %s", mint)
        return None
    if not blocks:
        return None
    path = append_session(
        mint,
        symbol,
        reason,
        "\n\n".join(blocks),
        directory=directory,
    )
    if path is not None:
        # written. dont append the same turns again next time.
        try:
            memory.clear_turns(mint)
        except Exception:  # noqa: BLE001
            log.exception("transcript clear failed %s", mint)
    return path
