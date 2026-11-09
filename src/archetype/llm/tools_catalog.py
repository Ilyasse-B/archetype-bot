"""
callable tool catalogue for the trader prompt + grammar.

vamp stays a fed info line — not in CALLABLE_TOOLS.
"""

from __future__ import annotations

from typing import Any

# research tools the model may set on the "tool" field
CALLABLE_TOOLS: tuple[str, ...] = ("metrics", "safety", "dex", "wallets")

_TOOL_ALIASES = {
    "onchain": "metrics",
    "rugcheck": "safety",
    "dexscreener": "dex",
    "dex_paid": "dex",
    "tracked": "wallets",
    "tracked_wallets": "wallets",
}

# one-line blurbs + ranks (catalogue text)
_TOOL_LINES: dict[str, str] = {
    "metrics": (
        "metrics - 0.5 - holders, top holders, and the last 5 minutes of trades. "
        "Use when you want that flow data — not for market cap (mcap is already on every card)."
    ),
    "safety": (
        "safety - 0.8 - rug score, insider networks, locked supply, bundlers, and fees. "
        "Useful before a buy, or again after a long hold, when you want a risk read."
    ),
    "dex": (
        "dex - 0.25 - whether the coin paid for a dexscreener profile, and how many "
        "boosts are applied. A sign the people running it care about the coin and "
        "improves visibility and trust."
    ),
    "wallets": (
        "wallets - 0.4 - how many tracked wallets currently hold this coin, and their "
        "average rating (0-1, the higher better). This means other well performing "
        "traders currently hold this coin."
    ),
}

_VAMP_LINE = (
    "vamp - 0.9 - fed alert (not callable) when a new migrated coin looks like the "
    "same narrative as your coin (name/ticker/socials). evaluate it; if it is the "
    "same narrative and looks stronger, sell the current coin in focus if you are "
    "holding (or reject if you have not bought yet) and move on. if it is not "
    "stronger, keep your coin."
)


def norm_tool_name(value: Any) -> str | None:
    """normalize a tool field to a callable name, or None if empty."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    return _TOOL_ALIASES.get(key, key)


def is_callable_tool(name: str | None) -> bool:
    """true when name is a real agent-callable tool."""
    return bool(name) and name in CALLABLE_TOOLS


def build_tools_section(
    allowed_tools: list[str] | tuple[str, ...] | None = None,
    *,
    phase: str = "searching",
) -> str:
    """
    tools block for the system prompt.

    only lists tools in allowed_tools (suppress drops one). vamp always shown.
    """
    if allowed_tools is None:
        allowed = list(CALLABLE_TOOLS)
    else:
        allowed = [t for t in allowed_tools if t in CALLABLE_TOOLS]

    lines = [
        "Tools. Set \"tool\" to one of these names (not \"action\"). One tool at a "
        "time. A tool only fetches info — it does not buy, sell, wait, reject, hold, "
        "or change your levels by itself. You may set \"tool\" alone, or together "
        "with an action. If you set both, we run the action and fetch the tool; the "
        "tool result appears on your next think.",
        "After you use a tool, it is omitted from this list for the next think.",
    ]
    for name in CALLABLE_TOOLS:
        if name not in allowed:
            continue
        blurb = _TOOL_LINES[name]
        if phase == "locked" and name == "safety":
            blurb = blurb.replace(
                "Useful before a buy, or again after a long hold, when you want a risk read.",
                "Useful after a long hold when you want a fresh risk read.",
            )
        lines.append(blurb)
    lines.append(_VAMP_LINE)
    # examples — only for tools still allowed
    if "metrics" in allowed:
        lines.append(
            '{"tool":"metrics","reasoning":"why you want a fresh metrics read"}'
        )
    if "safety" in allowed:
        lines.append(
            '{"action":"wait","tool":"safety","reasoning":"why wait and why this tool",'
            '"narrative":"what the coin is about"}'
            if phase != "locked"
            else '{"action":"hold","tool":"safety","reasoning":"why this tool while holding"}'
        )
    if "wallets" in allowed and phase != "locked":
        lines.append(
            '{"action":"buy","tool":"wallets","reasoning":"why buy and why this tool",'
            '"narrative":"what the coin is about","profit_pct":X,"loss_pct":Y}'
        )
    elif "dex" in allowed and phase == "locked":
        lines.append(
            '{"action":"hold","tool":"dex","reasoning":"why check boosts while holding"}'
        )
    return "\n".join(lines)
