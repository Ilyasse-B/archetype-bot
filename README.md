# Archetype

Local Solana memecoin agent. One coin at a time, paper fills only.

## Status

Phase 4 is running: a LangGraph loop on local Ollama `llama3.2:3b`. It searches (buy, reject, or wait), then holds, revises levels, or sells, and writes a thesis when the trade closes.

Phase 5 is in progress. Built: market-cap move since the last peek, live metrics, safety, and a Dexscreener check. Still ahead: tracked wallets, vamp, backlog peek, website, X, and Gemini amongst others.

## Setup

```bash
uv sync
cp .env.example .env
```

Fill `ARCHETYPE_HELIUS_API_KEY` and `ARCHETYPE_SOLANA_TRACKER_API_KEY` in `.env`.

## Run

```bash
systemctl --user start ollama
uv run python -m archetype.daemon
curl -fsS -X POST http://127.0.0.1:8765/agent/loop/start
uv run archetype migrations --follow
```

The daemon does not start the agent loop on its own.

Stop with:

```bash
curl -fsS -X POST http://127.0.0.1:8765/agent/loop/stop
uv run archetype stop
systemctl --user stop ollama
```

If port 8765 is already taken, an old daemon is still up. `uv run archetype stop` clears it.

## CLI

The CLI talks to the daemon.

```bash
uv run archetype status
uv run archetype migrations --follow
uv run archetype migrations --filtered --trade
uv run archetype backlog
uv run archetype peek <MINT>
uv run archetype watch <MINT>
uv run archetype buy <MINT> <SOL>
uv run archetype sell <MINT>
uv run archetype set-profit <MINT> <PCT>
uv run archetype set-loss <MINT> <PCT>
```
