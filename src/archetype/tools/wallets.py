"""
tracked wallets for the coin in focus.

helius holders ∩ local kol list. agent only sees count + avg rating.
no names, no addresses.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any

from solders.pubkey import Pubkey

from archetype.filters.helius import HeliusRpc

log = logging.getLogger("archetype.tools.wallets")

_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# cache the rating map so we dont reopen sqlite every call
_lock = threading.Lock()
_cache: dict[str, Any] = {"path": None, "mtime": None, "ratings": {}}


def fetch_wallets(
    mint: str,
    *,
    api_key: str = "",
    db_path: Path | str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """
    holders of this mint that sit on our kol list.

    returns count, avg_rating, and hits (hits stay off the agent text).
    a miss leaves count/avg as None.
    """
    mint = (mint or "").strip()
    empty: dict[str, Any] = {"count": None, "avg_rating": None, "hits": []}
    if not mint:
        return empty
    key = (api_key or "").strip()
    if not key:
        log.warning("wallets: no helius key")
        return empty
    path = Path(db_path) if db_path else Path()
    ratings = _ratings(path)
    if not ratings:
        log.warning("wallets: empty kol list at %s", path)
        return empty
    try:
        rpc = HeliusRpc(key, timeout=timeout)
        owners = _holders(rpc, mint)
    except Exception as exc:  # noqa: BLE001
        log.warning("wallets fetch failed %s: %s", mint, exc)
        return empty
    # one wallet can show up twice (weird atas). keep the first hit.
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for owner, amount in owners:
        if owner in seen or owner not in ratings:
            continue
        seen.add(owner)
        row = ratings[owner]
        hits.append(
            {
                "wallet": owner,
                "name": row.get("name") or "",
                "rating": float(row["rating"]),
                "amount": amount,
            }
        )
    if not hits:
        return {"count": 0, "avg_rating": None, "hits": []}
    avg = sum(h["rating"] for h in hits) / len(hits)
    return {"count": len(hits), "avg_rating": round(avg, 4), "hits": hits}


def wallets_text(read: dict[str, Any], explain: bool = False) -> str:
    """count + avg only. notes on the first ask."""
    count = read.get("count")
    avg = read.get("avg_rating")
    if count is None:
        return "wallets: unavailable"
    avg_s = "none" if avg is None else f"{float(avg):.2f}"
    lines = [
        "wallets:",
        _said(
            "tracked",
            str(int(count)),
            "number of tracked wallets holding this coin",
            explain,
        ),
        _said(
            "avg_rating",
            avg_s,
            "the average rating of those wallets from 0-1",
            explain,
        ),
    ]
    return "\n".join(lines)


def _said(label: str, value: str, note: str, explain: bool) -> str:
    if not explain:
        return f"{label}: {value}"
    return f"{label}: {value} ({note})"


def _ratings(path: Path) -> dict[str, dict[str, Any]]:
    """wallet -> {name, rating}. reloads when the db mtime changes."""
    if not path.is_file():
        return {}
    mtime = path.stat().st_mtime
    with _lock:
        if _cache["path"] == str(path) and _cache["mtime"] == mtime:
            return _cache["ratings"]
        out: dict[str, dict[str, Any]] = {}
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT wallet, name, rating FROM kol_wallets"
                ).fetchall()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("wallets db read failed %s: %s", path, exc)
            return {}
        for wallet, name, rating in rows:
            if not wallet or rating is None:
                continue
            try:
                out[str(wallet)] = {
                    "name": str(name or ""),
                    "rating": float(rating),
                }
            except (TypeError, ValueError):
                continue
        _cache["path"] = str(path)
        _cache["mtime"] = mtime
        _cache["ratings"] = out
        return out


def _holders(rpc: HeliusRpc, mint: str) -> list[tuple[str, int]]:
    """
    every token account for this mint with amount > 0.

    one getProgramAccounts. dataSlice = owner + amount so the body stays small.
    """
    program = _token_program(rpc, mint)
    cfg: dict[str, Any] = {
        "encoding": "base64",
        "commitment": "confirmed",
        "filters": [
            {"memcmp": {"offset": 0, "bytes": mint}},
        ],
        "dataSlice": {"offset": 32, "length": 40},
    }
    # classic spl atas are 165. token-2022 can be bigger, so skip the size filter there.
    if program == _TOKEN:
        cfg["filters"].insert(0, {"dataSize": 165})
    rows = rpc.call("getProgramAccounts", [program, cfg]) or []
    out: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data = ((row.get("account") or {}).get("data") or [None])[0]
        if not isinstance(data, str):
            continue
        try:
            raw = base64.b64decode(data)
        except Exception:  # noqa: BLE001
            continue
        if len(raw) < 40:
            continue
        owner = str(Pubkey.from_bytes(raw[0:32]))
        amount = struct.unpack_from("<Q", raw, 32)[0]
        if amount <= 0:
            continue
        out.append((owner, int(amount)))
    return out


def _token_program(rpc: HeliusRpc, mint: str) -> str:
    """tokenkeg or token-2022. default classic if the mint read fails."""
    try:
        info = rpc.get_account_info(mint, encoding="base64")
    except Exception:  # noqa: BLE001
        return _TOKEN
    if not isinstance(info, dict):
        return _TOKEN
    owner = str(info.get("owner") or "")
    if owner == _TOKEN_2022:
        return _TOKEN_2022
    return _TOKEN
