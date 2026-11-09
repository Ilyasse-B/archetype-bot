"""
build the coin-specific block injected into trader.md.

only fields that exist are listed, each with a short inline note
so the model does not need a separate card or a catalogue of possibles.
"""

from __future__ import annotations

from typing import Any

_WSOL = "So11111111111111111111111111111111111111112"

# snapshot keys → what the line means for the agent
_SNAPSHOT_NOTES: dict[str, str] = {
    "ath_mcap_usd": "all-time high market cap",
    "volume_1h_usd": "trading volume in the last hour (USD)",
    "liquidity_usd": "pool liquidity (USD)",
    "created": "when the coin was created",
    "coin_type": "how fees / rewards are set up on this coin",
    "fee_split": "where creator fees are redirected",
    "agent_buyback": "share of fees used for tokenized-agent buyback",
    "charity": "charity beneficiary tied to this coin",
    "charity_mission": "what that charity does",
    "charity_website": "charity website",
}


def render_coin_block(
    *,
    mint: str | None,
    coin: dict[str, Any] | None,
    buy_sol: float | None = None,
    snapshot: str = "",
) -> str:
    """
    one line per present field: `key: value — note`.

    empty coin still returns a mint line so the prompt is never blank.
    """
    coin = coin if isinstance(coin, dict) else {}
    lines: list[str] = []

    def add(key: str, value: Any, note: str) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not text or text in {"[]", "None"}:
            return
        lines.append(f"{key}: {text} — {note}")

    add("mint", mint or coin.get("mint"), "the coin's address")
    add("name", coin.get("name"), "display name")
    add("symbol", coin.get("symbol"), "ticker")

    socials = coin.get("socials_present") or []
    if isinstance(socials, list) and socials:
        add("socials", ", ".join(str(s) for s in socials), "linked socials we found")

    amm = coin.get("amm") or _amm_from_venue(coin.get("venue"))
    add("amm", amm, "graduated AMM (pumpswap, raydium, or meteora)")

    launchpad = (
        coin.get("launchpad")
        or coin.get("platform")
        or _launchpad_fallback(coin.get("venue"), mint or coin.get("mint"))
    )
    add("launchpad", launchpad, "where this coin was launched")

    if _is_paired(coin):
        add(
            "pairing",
            _pairing_label(coin),
            "the token or stock that this coin is paired to",
        )

    if coin.get("otc") and coin.get("otc_basket"):
        basket = coin.get("otc_basket")
        if isinstance(basket, list) and basket:
            add(
                "otc_basket",
                ", ".join(str(x) for x in basket),
                "holders will be paid rewards through the stocks in this basket",
            )

    desc = str(coin.get("description") or "").strip()
    if desc:
        add("description", desc[:400], "from the token metadata")

    if buy_sol is not None:
        add(
            "required_buy_sol",
            buy_sol,
            "the amount of SOL that will be bought once you decide to buy",
        )

    for key, value in _parse_snapshot(snapshot):
        note = _SNAPSHOT_NOTES.get(key, "from the coin snapshot")
        add(key, value, note)

    if not lines:
        return "mint: unknown — the coin's address"
    return "\n".join(lines)


def assemble_trader_prompt(
    template: str,
    *,
    phase: str,
    coin_block: str,
    allowed_tools: list[str] | tuple[str, ...] | None = None,
) -> str:
    """
    inject the coin block, tools catalogue, and keep only the matching phase.

    searching → buy/reject/wait block. locked → hold/revise/sell block.
    allowed_tools filters the callable catalogue (one-think suppress).
    """
    from archetype.llm.tools_catalog import build_tools_section

    text = template.replace(
        "{{COIN}}",
        (coin_block or "").strip() or "mint: unknown — the coin's address",
    )
    if phase == "locked":
        text = _keep_phase(text, "PHASE_LOCKED")
        text = _drop_phase(text, "PHASE_SEARCHING")
    else:
        text = _keep_phase(text, "PHASE_SEARCHING")
        text = _drop_phase(text, "PHASE_LOCKED")
    tools_block = build_tools_section(allowed_tools, phase=phase)
    if "{{TOOLS}}" in text:
        text = text.replace("{{TOOLS}}", tools_block)
    else:
        # older templates without the marker — append before trailing rules if any
        text = text.rstrip() + "\n\n" + tools_block
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _keep_phase(text: str, name: str) -> str:
    """unwrap {{NAME}}...{{/NAME}} keeping the inner body."""
    start = f"{{{{{name}}}}}"
    end = f"{{{{/{name}}}}}"
    if start not in text or end not in text:
        return text
    before, rest = text.split(start, 1)
    body, after = rest.split(end, 1)
    return before + body.strip("\n") + after


def _drop_phase(text: str, name: str) -> str:
    """remove {{NAME}}...{{/NAME}} entirely."""
    start = f"{{{{{name}}}}}"
    end = f"{{{{/{name}}}}}"
    if start not in text or end not in text:
        return text
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    return before + after


def _parse_snapshot(snapshot: str) -> list[tuple[str, str]]:
    """turn snapshot text lines into (key, value) pairs."""
    out: list[tuple[str, str]] = []
    for raw in (snapshot or "").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        if line.startswith("coin snapshot:"):
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key or not val:
            continue
        # charity lines may repeat the key prefix
        out.append((key, val))
    return out


def _is_paired(coin: dict[str, Any]) -> bool:
    quote = coin.get("quote_mint")
    return bool(quote) and quote != _WSOL


def _pairing_label(coin: dict[str, Any]) -> str:
    name = (coin.get("quote_name") or "").strip() or None
    symbol = (coin.get("quote_symbol") or "").strip() or None
    if name and symbol:
        return f"{name} ({symbol})"
    return name or symbol or "unknown"


def _amm_from_venue(venue: Any) -> str | None:
    from archetype.filters.thresholds import AMM_BY_VENUE

    if not venue:
        return None
    return AMM_BY_VENUE.get(str(venue), str(venue))


def _launchpad_fallback(venue: Any, mint: Any) -> str | None:
    from archetype.ingest.platform import launchpad_for_event

    if not venue:
        return None
    return launchpad_for_event(
        venue=str(venue),
        platform=None,
        mint=str(mint) if mint else None,
    )
