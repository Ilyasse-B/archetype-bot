"""
first-think coin snapshot for the agent card.

sources (no pump.fun http):
  jupiter lite-api — volume 1h, liquidity, created
  solana tracker  — ATH, cashback / holder-reward flags
  donate.gg       — charity detection + details
  helius          — fee-split + tokenized-agent PDAs
livestream is not available without pump.fun; that line is omitted.
"""

from __future__ import annotations

import base64
import logging
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx
from solders.pubkey import Pubkey

from archetype.filters.helius import HeliusRpc

log = logging.getLogger("archetype.metadata.snapshot")

_ACCEPT = {"Accept": "application/json"}
_DONATE_TOKEN = "https://www.donate.gg/api/v1/101/tokens/{mint}"
_JUPITER = "https://lite-api.jup.ag/tokens/v2/search"
_TRACKER = "https://data.solanatracker.io/tokens/{mint}"
_TRACKER_ATH = "https://data.solanatracker.io/tokens/{mint}/ath"

# pump fees program. sharing-config pda lives here.
_PUMP_FEES = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
# pump agent payments. token-agent-payments pda lives here.
_AGENT_PROGRAM = "AgenTMiC2hvxGebTsgmsD4HHBa8WEcqGFf87iwRRxLo7"

# anchor account discriminators from the pump sdk idl
_SHARING_DISC = bytes([216, 74, 9, 0, 56, 140, 93, 75])
_AGENT_DISC = bytes([136, 241, 242, 217, 173, 77, 112, 186])


def fetch_coin_snapshot(
    mint: str,
    rpc: HeliusRpc | None = None,
    tracker_api_key: str = "",
    timeout: float = 12.0,
    *,
    otc: bool = False,
) -> str:
    """
    text block for the first prompt.

    fails soft. a miss on one source leaves that line unknown / omitted.
    otc=True → coin_type is "OTC stocks basket" and fee_split is omitted
    (that wallet is just otc's reward router, not useful to the agent).
    """
    mint = (mint or "").strip()
    if not mint:
        return "coin snapshot: unavailable"

    jup, tracker, ath, donate = _fetch_http(mint, tracker_api_key, timeout)

    charity = False
    if isinstance(donate, dict) and (donate.get("activeConfig") or {}).get("charities"):
        charity = True
    else:
        donate = None

    cashback = False
    holder_reward = False
    if isinstance(tracker, dict):
        cashback, holder_reward = _pump_flags(tracker)

    fee_split: list[tuple[str, int]] | None = None
    agent_bps: int | None = None
    if rpc is not None and rpc.enabled:
        fee_split, agent_bps = _onchain_modes(rpc, mint)

    # otc fee share points at their reward wallet — hide it from the card
    if otc:
        fee_split = None

    have_market = isinstance(jup, dict) or isinstance(tracker, dict) or isinstance(ath, dict)
    if not have_market and not charity and not fee_split and agent_bps is None and not otc:
        return "coin snapshot: unavailable"

    return _render(
        jup=jup if isinstance(jup, dict) else None,
        tracker=tracker if isinstance(tracker, dict) else None,
        ath=ath if isinstance(ath, dict) else None,
        donate=donate,
        charity=charity,
        cashback=cashback,
        holder_reward=holder_reward,
        fee_split=fee_split,
        agent_bps=agent_bps,
        modes_known=isinstance(tracker, dict),
        otc=otc,
    )


def _fetch_http(
    mint: str, tracker_api_key: str, timeout: float
) -> tuple[Any, Any, Any, Any]:
    """jupiter + tracker token + tracker ath + donate.gg in parallel."""
    th: dict[str, str] = dict(_ACCEPT)
    key = (tracker_api_key or "").strip()
    if key:
        th["x-api-key"] = key

    def jup() -> Any:
        return _get_jupiter(mint, timeout)

    def tok() -> Any:
        if not key:
            return None
        return _get_json(_TRACKER.format(mint=mint), timeout, th)

    def ath() -> Any:
        if not key:
            return None
        return _get_json(_TRACKER_ATH.format(mint=mint), timeout, th)

    def don() -> Any:
        return _get_json(_DONATE_TOKEN.format(mint=mint), timeout, _ACCEPT)

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_jup = pool.submit(jup)
        f_tok = pool.submit(tok)
        f_ath = pool.submit(ath)
        f_don = pool.submit(don)
        return f_jup.result(), f_tok.result(), f_ath.result(), f_don.result()


def _get_jupiter(mint: str, timeout: float) -> dict[str, Any] | None:
    """lite-api search returns a list; pick the exact mint row."""
    try:
        resp = httpx.get(
            _JUPITER,
            params={"query": mint},
            headers=_ACCEPT,
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("jupiter snapshot failed %s: %s", mint[:8], exc)
        return None
    if resp.status_code != 200:
        log.warning("jupiter snapshot %s -> %s", mint[:8], resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, list):
        return None
    for row in body:
        if isinstance(row, dict) and row.get("id") == mint:
            return row
    return body[0] if body and isinstance(body[0], dict) else None


def _pump_flags(tracker: dict[str, Any]) -> tuple[bool, bool]:
    """read isCashbackCoin / isHolderReward off pumpfun-amm pool rows."""
    cashback = False
    holder_reward = False
    for pool in tracker.get("pools") or []:
        if not isinstance(pool, dict):
            continue
        flags = pool.get("pumpfun-amm")
        if not isinstance(flags, dict):
            continue
        if flags.get("isCashbackCoin") is True:
            cashback = True
        if flags.get("isHolderReward") is True:
            holder_reward = True
    return cashback, holder_reward


def _render(
    *,
    jup: dict[str, Any] | None,
    tracker: dict[str, Any] | None,
    ath: dict[str, Any] | None,
    donate: dict[str, Any] | None,
    charity: bool,
    cashback: bool,
    holder_reward: bool,
    fee_split: list[tuple[str, int]] | None,
    agent_bps: int | None,
    modes_known: bool,
    otc: bool = False,
) -> str:
    """short lines for the model."""
    ath_usd = None
    if isinstance(ath, dict):
        ath_usd = ath.get("highest_market_cap")

    vol_1h = _volume_1h(jup)
    liq = _liquidity(jup, tracker)
    created = _created_label(jup, tracker)

    lines = [
        f"ath_mcap_usd: {_usd(ath_usd)}",
        f"volume_1h_usd: {_usd(vol_1h)}",
        f"liquidity_usd: {_usd(liq)}",
        f"created: {created}",
        f"coin_type: {_coin_type(cashback, holder_reward, charity, fee_split, agent_bps, modes_known, otc=otc)}",
    ]
    if fee_split:
        bits = ", ".join(f"{addr} {bps / 100:g}%" for addr, bps in fee_split)
        lines.append(f"fee_split: {bits}")
    if agent_bps is not None:
        lines.append(f"agent_buyback: {agent_bps / 100:g}%")
    if charity:
        lines.extend(_charity_lines(donate))
    return "\n".join(lines)


def _volume_1h(jup: dict[str, Any] | None) -> float | None:
    if not jup:
        return None
    stats = jup.get("stats1h")
    if not isinstance(stats, dict):
        return None
    buy = stats.get("buyVolume")
    sell = stats.get("sellVolume")
    if not isinstance(buy, (int, float)) or not isinstance(sell, (int, float)):
        return None
    if isinstance(buy, bool) or isinstance(sell, bool):
        return None
    return float(buy) + float(sell)


def _liquidity(
    jup: dict[str, Any] | None, tracker: dict[str, Any] | None
) -> float | None:
    if jup and isinstance(jup.get("liquidity"), (int, float)) and not isinstance(
        jup.get("liquidity"), bool
    ):
        return float(jup["liquidity"])
    if not tracker:
        return None
    best = 0.0
    found = False
    for pool in tracker.get("pools") or []:
        if not isinstance(pool, dict):
            continue
        liq = pool.get("liquidity")
        usd = None
        if isinstance(liq, dict):
            usd = liq.get("usd")
        elif isinstance(liq, (int, float)) and not isinstance(liq, bool):
            usd = liq
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            found = True
            best = max(best, float(usd))
    return best if found else None


def _created_label(
    jup: dict[str, Any] | None, tracker: dict[str, Any] | None
) -> str:
    if jup and isinstance(jup.get("createdAt"), str) and jup["createdAt"].strip():
        return _format_created(jup["createdAt"])
    if tracker:
        creation = (tracker.get("token") or {}).get("creation") or {}
        if isinstance(creation, dict) and creation.get("created_time") is not None:
            return _format_created(creation.get("created_time"))
    return "unknown"


def _coin_type(
    cashback: bool,
    holder_reward: bool,
    charity: bool,
    fee_split: list[tuple[str, int]] | None,
    agent_bps: int | None,
    modes_known: bool,
    *,
    otc: bool = False,
) -> str:
    """which fee / launch modes are on. mayhem left out on purpose."""
    # otc replaces the generic "fee redirect" label — basket is on the card
    if otc:
        return "OTC stocks basket"
    kinds: list[str] = []
    if cashback:
        kinds.append("cashback")
    if holder_reward:
        kinds.append("holder rewards")
    if charity:
        kinds.append("charity")
    if fee_split:
        kinds.append("fee redirect")
    if agent_bps is not None:
        kinds.append("tokenized agent")
    if not kinds:
        # tracker answered with no special flags → normal creator fees
        return "creator fees" if modes_known else "unknown"
    return ", ".join(kinds)


def _charity_lines(donate: dict[str, Any] | None) -> list[str]:
    """name, mission, site. skip the street address and tax id."""
    if not donate:
        return ["charity: lookup failed"]
    active = donate.get("activeConfig") or {}
    charities = active.get("charities") or []
    if not isinstance(charities, list) or not charities:
        return ["charity: no beneficiary listed"]
    lines: list[str] = []
    for raw in charities:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip() or "unknown charity"
        weight = str(raw.get("weight") or "").strip()
        head = name if not weight else f"{name} (weight {weight})"
        lines.append(f"charity: {head}")
        mission = str(raw.get("mission") or "").strip()
        if mission:
            lines.append(f"charity_mission: {mission}")
        website = str(raw.get("website") or "").strip()
        if website:
            lines.append(f"charity_website: {website}")
        country = str(raw.get("country") or "").strip()
        if country:
            lines.append(f"charity_country: {country}")
    return lines or ["charity: no beneficiary listed"]


def _onchain_modes(
    rpc: HeliusRpc, mint: str
) -> tuple[list[tuple[str, int]] | None, int | None]:
    """sharing-config shareholders, and agent buyback bps if that account exists."""
    try:
        sharing = _pda(mint, b"sharing-config", _PUMP_FEES)
        agent = _pda(mint, b"token-agent-payments", _AGENT_PROGRAM)
        accounts = rpc.get_multiple_accounts([sharing, agent])
    except Exception as exc:  # noqa: BLE001
        log.warning("mode accounts failed for %s: %s", mint, exc)
        return None, None
    fee_split = _decode_sharing(accounts[0] if accounts else None)
    agent_bps = _decode_agent(accounts[1] if len(accounts) > 1 else None)
    return fee_split, agent_bps


def _pda(mint: str, seed: bytes, program: str) -> str:
    addr, _ = Pubkey.find_program_address(
        [seed, bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(program),
    )
    return str(addr)


def _account_bytes(account: dict[str, Any] | None) -> bytes | None:
    if not account:
        return None
    data = account.get("data")
    if isinstance(data, list) and data:
        try:
            return base64.b64decode(data[0])
        except Exception:  # noqa: BLE001
            return None
    if isinstance(data, str):
        try:
            return base64.b64decode(data)
        except Exception:  # noqa: BLE001
            return None
    return None


def _decode_sharing(account: dict[str, Any] | None) -> list[tuple[str, int]] | None:
    """
    shareholders when fees are actually redirected.

    a missing account, or the default 100% to the admin, is not a redirect.
    """
    raw = _account_bytes(account)
    if raw is None or len(raw) < 80 or raw[:8] != _SHARING_DISC:
        return None
    admin = str(Pubkey.from_bytes(raw[43:75]))
    (count,) = struct.unpack_from("<I", raw, 76)
    if count > 10:
        return None
    offset = 80
    shares: list[tuple[str, int]] = []
    for _ in range(count):
        if offset + 34 > len(raw):
            return None
        addr = str(Pubkey.from_bytes(raw[offset : offset + 32]))
        (bps,) = struct.unpack_from("<H", raw, offset + 32)
        shares.append((addr, int(bps)))
        offset += 34
    if not shares:
        return None
    if len(shares) == 1 and shares[0][0] == admin and shares[0][1] == 10_000:
        return None
    return shares


def _decode_agent(account: dict[str, Any] | None) -> int | None:
    """buyback bps when the token-agent-payments account exists. else None."""
    raw = _account_bytes(account)
    if raw is None or len(raw) < 75 or raw[:8] != _AGENT_DISC:
        return None
    (bps,) = struct.unpack_from("<H", raw, 73)
    return int(bps)


def _get_json(
    url: str, timeout: float, headers: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """one get. 404 and junk are None, not a crash."""
    try:
        resp = httpx.get(
            url,
            headers=headers or _ACCEPT,
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot get failed %s: %s", url, exc)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        log.warning("snapshot get %s -> %s", url, resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


def _usd(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"{value:,.0f}"


def _format_created(value: Any) -> str:
    """iso string, unix seconds, or unix ms → utc label."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "unknown"
        try:
            # 2026-11-03T21:21:43Z
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            return "unknown"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    ts = float(value)
    if ts > 10_000_000_000:
        ts /= 1000.0
    try:
        when = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "unknown"
    return when.strftime("%Y-%m-%d %H:%M UTC")
