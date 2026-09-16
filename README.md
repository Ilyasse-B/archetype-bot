# Archetype

Local Solana memecoin trading agent (paper-first). Design docs live in `docs/`.

## Phase 3 (in progress)

Live PumpSwap FDV via Helius WS in the CLI; paper wallet buy/sell/balance via the **daemon**.

### Setup

```bash
cd /home/ilyasse/Archetype/archetype-bot
uv sync
```

### Run daemon (required for paper + migrations)

```bash
uv run python -m archetype.daemon
```

### CLI (talks to daemon, except live watch)

```bash
uv run archetype status
uv run archetype migrations              # RAW unfiltered
uv run archetype migrations --filtered   # PASS only
uv run archetype backlog                 # pending agent queue

# paper wallet (daemon)
uv run archetype balance
uv run archetype balance add 50
uv run archetype balance reset
uv run archetype buy <MINT> 5            # fill on daemon → optional local watch
uv run archetype buy <MINT> 5 --no-track
uv run archetype sell <MINT>
uv run archetype sell <MINT> 25
uv run archetype sell <MINT> init
uv run archetype sell <MINT> 5 sol

# live price (CLI ↔ Helius WS directly; PnL uses daemon position if open)
uv run archetype watch <MINT>
uv run archetype watch <MINT> --once

uv run archetype stop
```

If you see `address already in use`, an old daemon is still running:

```bash
uv run archetype stop
# or: ss -tlnp | grep 8765
```

See `docs/PROJECT_OVERVIEW.md` for architecture.
