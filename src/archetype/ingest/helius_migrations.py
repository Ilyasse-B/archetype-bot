"""
helius parsed-stream feed for launchlab + meteora dbc graduations.

subscribes migrate-only via parsedTransactionSubscribe on the gatekeeper
websocket (free / 1 credit per event). falls back to logsSubscribe if
parsed streams reject the filter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from archetype.filters.thresholds import LAUNCHLAB_PROGRAM, METEORA_DBC_PROGRAM
from archetype.ingest.buffer import MigrationBuffer
from archetype.ingest.models import MigrationEvent, Venue

log = logging.getLogger("archetype.helius_migrations")

FilterHook = Callable[[MigrationEvent], Awaitable[MigrationEvent]]

# names from raydium / meteora idls — case/separator flexible on helius side
_LAUNCHLAB_IX = ["migrate_to_cpswap", "MigrateToCpswap"]
_METEORA_IX = ["migration_damm_v2", "migrate_meteora_damm", "MigrationDammV2"]

_MIGRATE_LOG_NEEDLES = (
    "migrate_to_cpswap",
    "MigrateToCpswap",
    "migration_damm_v2",
    "migrate_meteora_damm",
)


class MigrationDeduper:
    """drop duplicate migrate sigs / mints across pumpportal + helius feeds."""

    def __init__(self, maxlen: int = 800) -> None:
        self._sigs: OrderedDict[str, None] = OrderedDict()
        self._mints: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def mark_signature(self, signature: str | None) -> bool:
        """true if this sig was already marked."""
        sig = (signature or "").strip()
        if not sig:
            return False
        hit = sig in self._sigs
        self._sigs[sig] = None
        self._sigs.move_to_end(sig)
        while len(self._sigs) > self._maxlen:
            self._sigs.popitem(last=False)
        return hit

    def mark_mint(self, mint: str | None) -> bool:
        """true if this mint was already marked (post-repair dedupe)."""
        m = (mint or "").strip()
        if not m or m == "pending":
            return False
        hit = m in self._mints
        self._mints[m] = None
        self._mints.move_to_end(m)
        while len(self._mints) > self._maxlen:
            self._mints.popitem(last=False)
        return hit

    def seen_or_add(self, *, signature: str | None, mint: str | None) -> bool:
        """true if sig OR mint already seen; marks both."""
        sig_hit = self.mark_signature(signature)
        mint_hit = self.mark_mint(mint)
        return sig_hit or mint_hit


class HeliusMigrationFeed:
    """background feed: launchlab + meteora migrates → buffer → filter hook."""

    def __init__(
        self,
        api_key: str,
        buffer: MigrationBuffer,
        *,
        enable_launchlab: bool = True,
        enable_meteora_dbc: bool = True,
        reconnect_sec: float = 3.0,
        on_migration: FilterHook | None = None,
        deduper: MigrationDeduper | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._buffer = buffer
        self._enable_launchlab = enable_launchlab
        self._enable_meteora = enable_meteora_dbc
        self._reconnect_sec = reconnect_sec
        self._on_migration = on_migration
        self._deduper = deduper or MigrationDeduper()
        self._task: asyncio.Task[None] | None = None
        self.connected = False
        self.last_error: str | None = None
        self.mode: str | None = None  # parsed | logs

    @property
    def enabled(self) -> bool:
        return bool(self._api_key) and (
            self._enable_launchlab or self._enable_meteora
        )

    def _programs(self) -> list[str]:
        out: list[str] = []
        if self._enable_launchlab:
            out.append(LAUNCHLAB_PROGRAM)
        if self._enable_meteora:
            out.append(METEORA_DBC_PROGRAM)
        return out

    def _instruction_names(self) -> list[str]:
        names: list[str] = []
        if self._enable_launchlab:
            names.extend(_LAUNCHLAB_IX)
        if self._enable_meteora:
            names.extend(_METEORA_IX)
        return names

    @property
    def parsed_ws_url(self) -> str:
        return f"wss://beta.helius-rpc.com/?api-key={self._api_key}"

    @property
    def logs_ws_url(self) -> str:
        return f"wss://mainnet.helius-rpc.com/?api-key={self._api_key}"

    async def start(self) -> None:
        if not self.enabled:
            self.connected = False
            self.last_error = "helius migrations disabled (no key / both feeds off)"
            log.info("helius migration feed disabled")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run_forever(), name="helius-migrations"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self.connected = False
        log.info("helius migration feed stopped")

    async def _run_forever(self) -> None:
        while True:
            try:
                await self._connect_parsed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = f"parsed: {exc}"
                log.warning(
                    "helius parsed migrations failed (%s) — trying logsSubscribe",
                    exc,
                )
                try:
                    await self._connect_logs()
                except asyncio.CancelledError:
                    raise
                except Exception as exc2:  # noqa: BLE001
                    self.connected = False
                    self.last_error = str(exc2)
                    log.warning(
                        "helius logs migrations disconnected: %s — retry in %.1fs",
                        exc2,
                        self._reconnect_sec,
                    )
                    await asyncio.sleep(self._reconnect_sec)

    async def _connect_parsed(self) -> None:
        programs = self._programs()
        names = self._instruction_names()
        log.info(
            "connecting helius parsedTransactionSubscribe programs=%s ix=%s",
            [p[:8] for p in programs],
            names,
        )
        async with websockets.connect(
            self.parsed_ws_url,
            ping_interval=20,
            ping_timeout=20,
            max_size=8_000_000,
            open_timeout=20,
        ) as ws:
            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "parsedTransactionSubscribe",
                "params": [
                    {
                        "programs": programs,
                        "instructionNames": names,
                        "includeCpi": True,
                        "includeFailed": False,
                    },
                    {"details": "matched", "commitment": "confirmed"},
                ],
            }
            await ws.send(json.dumps(req))
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            reply = json.loads(raw)
            if reply.get("error"):
                raise RuntimeError(reply["error"])
            self.connected = True
            self.mode = "parsed"
            self.last_error = None
            log.info(
                "helius parsed migrations connected (sub=%s)",
                reply.get("result"),
            )
            async for frame in ws:
                await self._handle_parsed_frame(frame)

    async def _connect_logs(self) -> None:
        """noisier fallback — filter client-side for migrate log lines."""
        programs = self._programs()
        log.info(
            "connecting helius logsSubscribe mentions=%s",
            [p[:8] for p in programs],
        )
        async with websockets.connect(
            self.logs_ws_url,
            ping_interval=20,
            ping_timeout=20,
            max_size=8_000_000,
            open_timeout=20,
        ) as ws:
            # one sub per program
            for i, prog in enumerate(programs, start=1):
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": i,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [prog]},
                                {"commitment": "confirmed"},
                            ],
                        }
                    )
                )
            # drain subscribe acks
            for _ in programs:
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if ack.get("error"):
                    raise RuntimeError(ack["error"])
            self.connected = True
            self.mode = "logs"
            self.last_error = None
            log.info("helius logs migrations connected")
            async for frame in ws:
                await self._handle_logs_frame(frame)

    async def _handle_parsed_frame(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("method") != "parsedTransactionNotification":
            return
        value = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
        tx_wrap = value.get("transaction") or {}
        sig = tx_wrap.get("signature") or value.get("signature")
        instructions = value.get("instructions") or []
        event = _event_from_parsed(sig, instructions)
        if event is None:
            # still enqueue a stub so repair can decode via getTransaction
            if not sig:
                return
            # guess venue from matched program ids
            venue = _venue_from_instructions(instructions)
            event = MigrationEvent(
                mint="pending",
                coin_mint="pending",
                venue=venue,
                signature=str(sig),
                raw={"source": "helius_parsed", "instructions": instructions},
            )
        await self._emit(event)

    async def _handle_logs_frame(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("method") != "logsNotification":
            return
        result = (msg.get("params") or {}).get("result") or {}
        value = result.get("value") or {}
        logs = value.get("logs") or []
        joined = "\n".join(str(x) for x in logs)
        if not any(n in joined for n in _MIGRATE_LOG_NEEDLES):
            return
        sig = value.get("signature")
        if not sig:
            return
        venue: Venue = "unknown"
        if LAUNCHLAB_PROGRAM[:8] in joined or "MigrateToCpswap" in joined or "migrate_to_cpswap" in joined:
            venue = "launchlab"
        elif "migration_damm" in joined or "migrate_meteora" in joined:
            venue = "meteora_dbc"
        event = MigrationEvent(
            mint="pending",
            coin_mint="pending",
            venue=venue,
            signature=str(sig),
            raw={"source": "helius_logs"},
        )
        await self._emit(event)

    async def _emit(self, event: MigrationEvent) -> None:
        """sig-dedupe → buffer → optional filter hook (mint dedupe lives in the hook)."""
        # suppress duplicate websocket deliveries of the same migrate sig
        if self._deduper.mark_signature(event.signature):
            log.debug("helius migrate dup skip sig=%s", event.signature)
            return

        stored = await self._buffer.add(event)
        log.info(
            "HELIUS MIGRATION #%s venue=%s sig=%s…",
            stored.id,
            stored.venue,
            (stored.signature or "?")[:16],
        )
        if self._on_migration is None:
            return
        try:
            updated = await self._on_migration(stored.model_copy(deep=True))
            await self._buffer.update(updated)
        except Exception as exc:  # noqa: BLE001
            log.exception("helius filter hook failed: %s", exc)
            failed = stored.model_copy(deep=True)
            failed.filter_passed = False
            failed.filter_reasons = [f"filter error: {exc}"]
            await self._buffer.update(failed)


def _venue_from_instructions(instructions: list[Any]) -> Venue:
    for ix in instructions:
        if not isinstance(ix, dict):
            continue
        pid = str(ix.get("programId") or "")
        if pid == LAUNCHLAB_PROGRAM:
            return "launchlab"
        if pid == METEORA_DBC_PROGRAM:
            return "meteora_dbc"
        decoded = ix.get("decoded") or {}
        accounts = decoded.get("accounts") if isinstance(decoded, dict) else None
        # some payloads nest program on decoded
        name = ""
        if isinstance(decoded, dict):
            name = str(decoded.get("name") or decoded.get("instruction") or "")
        if "cpswap" in name.lower() or "migrate_to" in name.lower():
            return "launchlab"
        if "damm" in name.lower() or "meteora" in name.lower():
            return "meteora_dbc"
        if isinstance(accounts, dict):
            pass
    return "unknown"


def _event_from_parsed(
    sig: Any, instructions: list[Any]
) -> MigrationEvent | None:
    """
    build an event when decoded accounts already name base/quote/pool.

    returns None if we only got raw matched ixs — repair will getTransaction.
    """
    for ix in instructions:
        if not isinstance(ix, dict):
            continue
        decoded = ix.get("decoded") or {}
        if not isinstance(decoded, dict):
            continue
        accounts = decoded.get("accounts") or {}
        if not isinstance(accounts, dict):
            continue
        # launchlab names
        base = accounts.get("base_mint") or accounts.get("baseMint")
        quote = accounts.get("quote_mint") or accounts.get("quoteMint")
        pool = (
            accounts.get("cpswap_pool")
            or accounts.get("pool")
            or accounts.get("damm_pool")
            or accounts.get("pool_state")
        )
        pid = str(ix.get("programId") or "")
        venue: Venue = "unknown"
        if pid == LAUNCHLAB_PROGRAM or "cpswap" in str(decoded.get("name") or "").lower():
            venue = "launchlab"
        elif pid == METEORA_DBC_PROGRAM or "damm" in str(decoded.get("name") or "").lower():
            venue = "meteora_dbc"
        if base and quote:
            return MigrationEvent(
                mint=str(base),
                coin_mint=str(base),
                quote_mint=str(quote),
                venue=venue if venue != "unknown" else _venue_from_instructions(instructions),
                pool=str(pool) if pool else None,
                signature=str(sig) if sig else None,
                raw={"source": "helius_parsed", "decoded": decoded},
            )
    return None
