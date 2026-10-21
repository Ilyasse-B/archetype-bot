# Archetype

Local Solana memecoin agent. One coin at a time, paper fills only.

## Status

Phase 4 is running: a LangGraph loop on local Ollama `llama3.2:3b`. It searches (buy, reject, or wait), then holds, revises levels, or sells, and writes a thesis when the trade closes.

Phase 5 is in progress. Built: market-cap move since the last peek, live metrics, safety, and a Dexscreener check. Still ahead: tracked wallets, vamp, backlog peek, website, X, and Gemini amongst others.

## How it works

New Solana coins show up when they migrate. Those arrivals come in through PumpPortal and land in a daemon that stays running.

The daemon looks the coin up on the chain through Helius and runs a fixed set of checks. Coins that fail are dropped. Coins that pass are handed to a local Ollama agent, along with the basic details of the coin.

The agent also has a catalogue of tools it can call when it wants to know more. That catalogue is still being built. With what it has, the agent can buy the coin or reject it.

After a buy, the agent keeps thinking in a loop. It can call tools again, update its view of the coin, and change its take-profit and stop-loss. The daemon watches those levels and closes the trade on its own once one of them is hit.

When the trade is over, the agent writes a short thesis: why it bought, and why it did what it did along the way.

Once the tool set is wider and the daemon and agent are in better shape, the agent will be left running for longer stretches so it can collect trading data. After about a week, that record will be used to train the model with LoRA, so the weights can get better at the job.

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
