"""
coins-v2 snapshot for the first agent card.

one http get, plus donate.gg when the coin is a charity, plus one helius
batch for fee-split and tokenized-agent. mayhem is left out on purpose.
"""

from __future__ import annotations

import base64
import logging
import struct
from datetime import datetime, timezone
from typing import Any

import httpx
from solders.pubkey import Pubkey

from archetype.filters.helius import HeliusRpc

log = logging.getLogger("archetype.metadata.snapshot")

_UA = "Mozilla/5.0 (compatible; ArchetypeBot/0.1; +https://localhost)"
_COINS_V2 = "https://frontend-api-v3.pump.fun/coins-v2/{mint}"
_DONATE_TOKEN = "https://www.donate.gg/api/v1/101/tokens/{mint}"

# pump fees program. sharing-config pda lives here.
_PUMP_FEES = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
# pump agent payments. token-agent-payments pda lives here.
_AGENT_PROGRAM = "AgenTMiC2hvxGebTsgmsD4HHBa8WEcqGFf87iwRRxLo7"

# anchor account discriminators from the pump sdk idl
_SHARING_DISC = bytes([216, 74, 9, 0, 56, 140, 93, 75])
_AGENT_DISC = bytes([136, 241, 242, 217, 173, 77, 112, 186])


def fetch_coin_snapshot(mint: str, rpc: HeliusRpc | None = None, timeout: float = 12.0) -> str:
    """
    text block for the first prompt. empty string if coins-v2 did not answer.

    fails soft. a bad donate.gg or helius read does not drop the rest.
    """
    mint = (mint or "").strip()
    if not mint:
        return "coin snapshot: unavailable"
    body = _get_json(_COINS_V2.format(mint=mint), timeout=timeout)
    have_http = isinstance(body, dict)
    if not have_http:
        log.warning("coins-v2 miss for %s", mint)
        body = {}

    charity = body.get("is_charity") is True
    donate: dict[str, Any] | None = None
    # no coins-v2 flag (or it said charity): ask donate.gg. 404 just means not a charity coin.
    if charity or not have_http:
        donate = _get_json(_DONATE_TOKEN.format(mint=mint), timeout=timeout)
        if isinstance(donate, dict) and (donate.get("activeConfig") or {}).get("charities"):
            charity = True
        else:
            donate = None

    fee_split: list[tuple[str, int]] | None = None
    agent_bps: int | None = None
    if rpc is not None and rpc.enabled:
        fee_split, agent_bps = _onchain_modes(rpc, mint)

    if not have_http and not charity and not fee_split and agent_bps is None:
        return "coin snapshot: unavailable"
    return _render(
        body,
        donate=donate,
        charity=charity,
        fee_split=fee_split,
        agent_bps=agent_bps,
        have_http=have_http,
    )


def _render(
    body: dict[str, Any],
    *,
    donate: dict[str, Any] | None,
    charity: bool,
    fee_split: list[tuple[str, int]] | None,
    agent_bps: int | None,
    have_http: bool = True,
) -> str:
    """turn the json into short lines. the model reads these, not the raw body."""
    lines = [
        f"ath_mcap_usd: {_usd(body.get('ath_market_cap'))}",
        f"volume_1h_usd: {_usd(body.get('volume_1h_usd'))}",
        f"liquidity_usd: {_usd(body.get('canonical_pool_liquidity_usd'))}",
        f"livestream: {_on_off(body.get('is_currently_live'))}",
        f"created: {_created(body.get('created_timestamp'))}",
        f"coin_type: {_coin_type(body, charity=charity, fee_split=fee_split, agent_bps=agent_bps, have_http=have_http)}",
    ]
    if fee_split:
        bits = ", ".join(f"{addr} {bps / 100:g}%" for addr, bps in fee_split)
        lines.append(f"fee_split: {bits}")
    if agent_bps is not None:
        lines.append(f"agent_buyback: {agent_bps / 100:g}%")
    if charity:
        lines.extend(_charity_lines(donate))
    return "\n".join(lines)


def _coin_type(
    body: dict[str, Any],
    *,
    charity: bool,
    fee_split: list[tuple[str, int]] | None,
    agent_bps: int | None,
    have_http: bool = True,
) -> str:
    """which fee / launch modes are actually on. mayhem is not one of them."""
    kinds: list[str] = []
    if body.get("is_cashback_enabled") is True:
        kinds.append("cashback")
    if body.get("is_holder_reward") is True:
        kinds.append("holder rewards")
    if charity:
        kinds.append("charity")
    if fee_split:
        kinds.append("fee redirect")
    if agent_bps is not None:
        kinds.append("tokenized agent")
    if not kinds:
        # coins-v2 never answered, so "creator fees" would be a guess
        return "creator fees" if have_http else "unknown"
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
    layout is the pump sharingConfig account (disc + bump, version, status,
    mint, admin, admin_revoked, then vec of pubkey + u16).
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
    # creator still has the whole fee. not a redirect.
    if len(shares) == 1 and shares[0][0] == admin and shares[0][1] == 10_000:
        return None
    return shares


def _decode_agent(account: dict[str, Any] | None) -> int | None:
    """buyback bps when the token-agent-payments account exists. else None."""
    raw = _account_bytes(account)
    # disc + bump + mint + authority + u16
    if raw is None or len(raw) < 75 or raw[:8] != _AGENT_DISC:
        return None
    (bps,) = struct.unpack_from("<H", raw, 73)
    return int(bps)


def _get_json(url: str, timeout: float) -> dict[str, Any] | None:
    """one get. 404 and junk are None, not a crash."""
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _UA, "Accept": "application/json"},
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


def _on_off(value: Any) -> str:
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "unknown"


def _created(value: Any) -> str:
    """coins-v2 sends milliseconds. older payloads sometimes send seconds."""
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
