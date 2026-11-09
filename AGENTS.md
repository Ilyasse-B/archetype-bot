# AGENTS.md — Archetype

Read this first when joining the repo. Then explore `docs/` before changing behaviour.

## What this is

Archetype is a **local** Solana memecoin trading agent. It watches migrations, runs deterministic safety filters, then hands survivors to a LangGraph agent on local Ollama (`llama3.2:3b`). The agent decides buy / reject / wait, can call research tools, sets TP/SL while holding, and writes a short thesis when a trade ends.

- **One coin at a time.** Backlog is FIFO after the focused trade closes.
- **Paper fills only** for now (PumpSwap, Raydium CPMM, Meteora DAMM v2).
- **Fast path** = code (ingest, filters, peeks, execution). **Slow path** = local LLM reasoning.
- Near-term operation is **CLI + daemon on the developer machine**, not cloud. Secrets and SQLite state live in gitignored local files (`.env`, `data/`, etc.).

There is also a small localhost **demo viewer** (`web/`, `docs/web-app.md`) as a side convenience around the daemon. Day-to-day work assumes CLI.

## Orientation — read the docs

Start here, in roughly this order:

| Doc | Why |
|-----|-----|
| `README.md` | Setup, run/stop, CLI |
| `docs/PROJECT_OVERVIEW.md` | Deep system picture (authoritative long-form) |
| `docs/PROJECT_PLAN.md` | Vision / architecture roadmap |
| `docs/all-tools.md` | Tool catalogue: built vs planned, fed vs agent |
| `docs/vamp-tool.md` | Vamp / copycat scanner (fed/interrupt; built) |
| `docs/other_streams.md` / `docs/other_streams_plan.md` | Extra migration venues |
| `docs/web-app.md` | Demo viewer (side path) |
| `docs/metadata.md`, `docs/ai_agent_architecture_blueprint.md`, others | Supporting detail |

Also skim LLM prompts under `src/archetype/llm/prompts/` (especially `trader.md`) and `src/archetype/llm/tools_catalog.py` — trade verbs on `action`, research on `tool` (see `docs/tool-calling-fixes.md`).

**Do not update `README.md` unless the user explicitly asks.** The README is a formality updated only after significant progress. It is not a core knowledge-base file or progress tracker. Prefer `docs/`, `AGENTS.md`, and Project Context for living knowledge.

**Git workflow (user-gated):** Stay on `main`. **Commit and push only when the user explicitly asks.** Do **not** create feature branches or pull requests unless the user explicitly asks. When committing, do not add Cursor / AI co-author trailers.

## Agent-facing language (keep it open)

Anything the trading model reads (`trader.md`, tool catalogue blurbs/examples in `tools_catalog.py`, fed event text, coach lines, thesis prompts, etc.) must **not** push it toward one fixed behaviour.

- Do **not** hardcode example numbers that look like the “right” choice (e.g. `profit_pct: 30`, `loss_pct: 10` in buy/revise examples). Use placeholders like `X` / `Y`, or spell out “a suitable …”, so the model picks levels from the coin and its own reasoning.
- Avoid forceful “always / must / whenever” nudges that collapse judgment (e.g. “call metrics whenever”, “always sell if …”) unless the product rule is truly mandatory and deterministic.
- Prefer describing **what a field means** and **when it can help**, not a script the agent should copy every time.

## Then read code if you need precision

Docs can lag. When implementing or debugging, follow the relevant code rather than guessing:

- `src/archetype/` — package root (daemon, agent loop, filters, trading, ingest, CLI)
- `config/default.yaml` — tunables; `.env` / `ARCHETYPE_*` overrides (see `.env.example`)
- `web/demo/` — demo UI only

Prefer updating markdown when behaviour changes (`docs/`, `AGENTS.md`). We keep those current so a full project report can be written from them later. Leave `README.md` alone unless asked (see note above).

## Roadmap (high level)

1. Build remaining tools — vamp scanner is in (`docs/vamp-tool.md`); next: backlog peek / website / X / Gemini.
2. Finish other planned tools, then refine daemon + agent for longer reliable runs.
3. Reset local DBs fresh and run for long stretches to collect training data.
4. Train / fine-tune so the model gets better at trading.

Do not invent scope outside the docs without an explicit ask.
