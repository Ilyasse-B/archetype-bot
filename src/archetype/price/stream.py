"""
live price stream — helius accountSubscribe on pool vaults (fast path),
with optional RPC poll fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import websockets

from archetype.filters.helius import HeliusRpc
from archetype.filters.sol_price import get_sol_usd
from archetype.price.models import PriceTick, build_tick
from archetype.price.resolve import resolve_pumpswap_pool
from archetype.price.snapshot import fetch_price_tick

log = logging.getLogger("archetype.price")


def iter_price_ticks(
    rpc: HeliusRpc,
    mint: str,
    *,
    interval_sec: float = 1.0,
    refresh_sol_every: int = 30,
) -> Iterator[PriceTick]:
    """RPC poll loop (fallback)."""
    resolved = resolve_pumpswap_pool(rpc, mint)
    log.info(
        "watching (poll) %s pool=%s name=%s",
        mint,
        resolved.pool.address,
        resolved.name or "?",
    )
    sol_usd = get_sol_usd(rpc)
    n = 0
    while True:
        if n > 0 and n % max(1, refresh_sol_every) == 0:
            try:
                sol_usd = get_sol_usd(rpc)
            except Exception as exc:  # noqa: BLE001
                log.warning("sol usd refresh failed: %s", exc)
        try:
            tick = fetch_price_tick(
                rpc, mint, resolved=resolved, sol_usd=sol_usd, source="rpc"
            )
            yield tick
        except Exception as exc:  # noqa: BLE001
            log.warning("price tick failed: %s", exc)
        n += 1
        time.sleep(max(0.2, interval_sec))


async def aiter_price_ticks_ws(
    rpc: HeliusRpc,
    mint: str,
    *,
    heartbeat_sec: float = 2.0,
    sol_refresh_sec: float = 30.0,
) -> AsyncIterator[PriceTick]:
    """
    subscribe to base + quote vault ATAs; emit a tick on every reserve change.

    also heartbeats every heartbeat_sec so the CLI isn't silent between trades.
    """
    if not rpc.enabled:
        raise RuntimeError("ARCHETYPE_HELIUS_API_KEY not set")

    resolved = resolve_pumpswap_pool(rpc, mint)
    pool = resolved.pool
    base_ata = pool.pool_base_token_account
    quote_ata = pool.pool_quote_token_account
    log.info(
        "watching (ws) %s pool=%s base=%s… quote=%s…",
        mint,
        pool.address,
        base_ata[:8],
        quote_ata[:8],
    )

    sol_usd = get_sol_usd(rpc)
    # seed balances via RPC so first tick is immediate
    tick0 = fetch_price_tick(rpc, mint, resolved=resolved, sol_usd=sol_usd, source="ws")
    base_ui = tick0.pool_base_ui
    quote_sol = tick0.pool_sol
    yield tick0

    ws_url = rpc.ws_url
    req_id = 0
    sub_to_kind: dict[int, str] = {}

    def _next_id() -> int:
        nonlocal req_id
        req_id += 1
        return req_id

    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
        for kind, ata in (("base", base_ata), ("quote", quote_ata)):
            mid = _next_id()
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "method": "accountSubscribe",
                        "params": [
                            ata,
                            {"encoding": "jsonParsed", "commitment": "confirmed"},
                        ],
                    }
                )
            )
            # map reply id → kind temporarily; subscription id comes in result
            sub_to_kind[mid] = kind

        id_to_kind: dict[int, str] = {}
        last_emit = time.monotonic()
        last_sol = time.monotonic()

        while True:
            timeout = max(0.2, heartbeat_sec)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except TimeoutError:
                # heartbeat — refresh sol occasionally, re-emit from cached reserves
                now = time.monotonic()
                if now - last_sol >= sol_refresh_sec:
                    try:
                        sol_usd = get_sol_usd(rpc)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("sol usd refresh failed: %s", exc)
                    last_sol = now
                try:
                    yield build_tick(
                        mint=mint,
                        pool=pool.address,
                        base_ui=base_ui,
                        quote_sol=quote_sol,
                        supply_ui=resolved.supply_ui,
                        decimals=resolved.decimals,
                        sol_usd=sol_usd,
                        name=resolved.name,
                        symbol=resolved.symbol,
                        source="ws-heartbeat",
                    )
                    last_emit = time.monotonic()
                except Exception as exc:  # noqa: BLE001
                    log.warning("heartbeat tick failed: %s", exc)
                continue

            try:
                msg: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # subscription ack: {"result": <sub_id>, "id": <req_id>}
            if "result" in msg and "id" in msg and "method" not in msg:
                kind = sub_to_kind.pop(int(msg["id"]), None)
                if kind is not None:
                    id_to_kind[int(msg["result"])] = kind
                continue

            if msg.get("method") != "accountNotification":
                continue

            params = msg.get("params") or {}
            sub_id = params.get("subscription")
            kind = id_to_kind.get(int(sub_id)) if sub_id is not None else None
            result = params.get("result") or {}
            slot = (result.get("context") or {}).get("slot")
            value = result.get("value") or {}
            amount_ui, amount_raw, decimals = _parse_token_notification(value)
            if kind == "base" and amount_ui is not None:
                base_ui = float(amount_ui)
            elif kind == "quote":
                if amount_raw is not None:
                    quote_sol = int(amount_raw) / 1e9
                elif amount_ui is not None and decimals is not None:
                    quote_sol = float(amount_ui)

            now = time.monotonic()
            if now - last_sol >= sol_refresh_sec:
                try:
                    sol_usd = get_sol_usd(rpc)
                except Exception as exc:  # noqa: BLE001
                    log.warning("sol usd refresh failed: %s", exc)
                last_sol = now

            try:
                yield build_tick(
                    mint=mint,
                    pool=pool.address,
                    base_ui=base_ui,
                    quote_sol=quote_sol,
                    supply_ui=resolved.supply_ui,
                    decimals=resolved.decimals,
                    sol_usd=sol_usd,
                    name=resolved.name,
                    symbol=resolved.symbol,
                    slot=int(slot) if slot is not None else None,
                    source="ws",
                )
                last_emit = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                log.warning("ws tick failed: %s", exc)


def _parse_token_notification(
    value: dict[str, Any],
) -> tuple[float | None, int | None, int | None]:
    """pull uiAmount / raw amount from jsonParsed token account notify."""
    data = value.get("data")
    if not isinstance(data, dict):
        return None, None, None
    parsed = data.get("parsed") or {}
    info = parsed.get("info") or {}
    ta = info.get("tokenAmount") or {}
    ui = ta.get("uiAmount")
    raw = ta.get("amount")
    dec = ta.get("decimals")
    try:
        ui_f = float(ui) if ui is not None else None
    except (TypeError, ValueError):
        ui_f = None
    try:
        raw_i = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        raw_i = None
    try:
        dec_i = int(dec) if dec is not None else None
    except (TypeError, ValueError):
        dec_i = None
    return ui_f, raw_i, dec_i
