# Archetype

Local Solana memecoin agent. One coin at a time, paper fills only.

## Status

The project is in progress.

**Already built:** a LangGraph agent loop on local Ollama `llama3.2:3b` (buy, reject, wait, hold, revise TP/SL, sell, thesis), migration ingest, deterministic filters (including Birdeye global-fees on every venue), paper trading across the supported AMMs, live peek / market-cap move since last peek, safety and Dexscreener checks, tracked wallets (KOL holder tool), OTC desks lookups, and a separate localhost demo viewer around the daemon.

**Still to do:** finish the remaining agent tools (vamp, backlog peek, website, X, Gemini, and others), then refine the daemon and agent so they can run reliably for longer stretches.

**After that:** leave the agent running to collect trading data, then train the model on that record (e.g. LoRA) so the weights get better at the job.

**Venues:** migrations from **PumpSwap**, **Raydium CPMM** (LaunchLab and any launchpad on it — Float, StonkFun, higher.fun, letsbonk, …), and **Meteora DAMM v2** (DBC / Bags, …). Paper fills work on all three AMMs. Fee-floor checks run on all three; bonding-curve deployer checks stay pump-specific. **OTC desks** is not an AMM: after a filter PASS we look up the mint on otcdesks; if it hits, the agent card gets the reward stock basket.

## How it works

New Solana coins show up when they migrate. Pump grads come through PumpPortal; LaunchLab and Meteora DBC grads come through Helius parsed streams. Both land in a daemon that stays running.

The daemon looks the coin up on the chain through Helius and runs a fixed set of checks. Coins that fail are dropped. Coins that pass are handed to a local Ollama agent, along with the basic details of the coin (amm, launchpad, quote pairing when non-SOL, and OTC basket when listed).

The agent also has a catalogue of tools it can call when it wants to know more. That catalogue is still being built. With what it has, the agent can buy the coin or reject it.

After a buy, the agent keeps thinking in a loop. It can call tools again, update its view of the coin, and change its take-profit and stop-loss. The daemon watches those levels and closes the trade on its own once one of them is hit.

When the trade is over, the agent writes a short thesis: why it bought, and why it did what it did along the way.

The tool catalogue is still being built. After the remaining tools and refinements land, the agent will run for longer periods to collect trading data, then that data will be used to train the model.

## Setup

```bash
uv sync
cp .env.example .env
```

Fill `ARCHETYPE_HELIUS_API_KEY`, `ARCHETYPE_SOLANA_TRACKER_API_KEY`, and `ARCHETYPE_BIRDEYE_API_KEY` in `.env`.

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
uv run archetype demo
```

## Demo viewer

Separate localhost UI for demos / recording (not part of the daemon). Starts Ollama, the daemon, and the agent loop from the browser, and shows the focused coin plus a live agent log.

```bash
uv run archetype demo          # http://127.0.0.1:8787/
```

See [`docs/web-app.md`](docs/web-app.md).
