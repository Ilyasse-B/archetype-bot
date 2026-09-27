"""
agent tool catalog — names/paths the Phase 4 LLM binder will load.

keep this the single source of truth for "what can the agent call".
"""

from __future__ import annotations

from archetype.tools.models import ToolSpec

# starter set only — vamp / linked-X / website check land later
TOOL_CATALOG: list[ToolSpec] = [
    ToolSpec(
        name="list_tools",
        description="List available agent tools and their schemas.",
        method="GET",
        path="/agent/tools",
        when="always",
    ),
    ToolSpec(
        name="peek_price",
        description=(
            "One-shot FDV/price snapshot for a mint (optional PnL if holding). "
            "Call often while locked — do not stream every tick."
        ),
        method="POST",
        path="/agent/peek",
        when="always",
        params={"mint": "token mint address"},
    ),
    ToolSpec(
        name="subscribe_move_alerts",
        description=(
            "Watch a mint on Helius vault WS; push an alert when FDV/price "
            "moves by threshold_pct up or down from the last baseline."
        ),
        method="POST",
        path="/agent/alerts/subscribe",
        when="locked",
        params={
            "mint": "token mint",
            "threshold_pct": "abs %% move to fire (default 10)",
        },
    ),
    ToolSpec(
        name="unsubscribe_move_alerts",
        description="Stop move-alert monitoring for a mint.",
        method="POST",
        path="/agent/alerts/unsubscribe",
        when="locked",
        params={"mint": "token mint"},
    ),
    ToolSpec(
        name="drain_move_alerts",
        description=(
            "Pull pending unsolicited move alerts (FIFO). "
            "Agent should drain these each think step."
        ),
        method="GET",
        path="/agent/alerts/pending",
        when="locked",
    ),
    ToolSpec(
        name="peek_backlog",
        description=(
            "Look at pending filter-passed migrations without consuming. "
            "Safe while locked for other-coin context."
        ),
        method="GET",
        path="/agent/backlog/peek",
        when="always",
        params={"limit": "max items (default 10)"},
    ),
    ToolSpec(
        name="next_candidate",
        description=(
            "Pop the oldest backlog item (FIFO) when FREE/searching. "
            "Marks it consumed."
        ),
        method="POST",
        path="/agent/backlog/next",
        when="free",
    ),
    ToolSpec(
        name="get_wallet",
        description="Paper cash balance + open positions (marked).",
        method="GET",
        path="/agent/wallet",
        when="always",
    ),
    ToolSpec(
        name="get_position",
        description="Open paper position + TP/SL for a mint (or null).",
        method="GET",
        path="/agent/position/{mint}",
        when="always",
    ),
    ToolSpec(
        name="buy",
        description=(
            "Paper buy. Requires profit_pct + loss_pct (TP/SL) before fill. "
            "Daemon starts TP/SL monitor after buy."
        ),
        method="POST",
        path="/agent/buy",
        when="free",
        params={
            "mint": "token mint",
            "sol": "SOL to spend",
            "profit_pct": "take-profit %%",
            "loss_pct": "stop-loss %% magnitude",
        },
    ),
    ToolSpec(
        name="sell",
        description="Paper sell (full / percent / init / sol_amount).",
        method="POST",
        path="/agent/sell",
        when="locked",
        params={
            "mint": "token mint",
            "percent": "optional %% of tokens",
            "sol_amount": "optional SOL out",
            "initials": "true = recover first-buy SOL",
        },
    ),
    ToolSpec(
        name="set_tpsl",
        description="Update or clear take-profit / stop-loss percentages.",
        method="POST",
        path="/agent/tpsl",
        when="locked",
        params={
            "mint": "token mint",
            "profit_pct": "optional new TP %%",
            "loss_pct": "optional new SL %%",
            "clear_profit": "bool",
            "clear_loss": "bool",
        },
    ),
    ToolSpec(
        name="reject",
        description="Reject a candidate (logged to agent_rejects, never revisit).",
        method="POST",
        path="/agent/reject",
        when="free",
        params={"mint": "token mint", "reasoning": "short why"},
    ),
]


def catalog_dicts() -> list[dict]:
    """json-friendly catalog for GET /agent/tools."""
    return [t.model_dump() for t in TOOL_CATALOG]
