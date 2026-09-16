# Archetype

Local Solana memecoin trading agent (paper-first). Design docs live in `docs/`.

## Phase 3 (in progress)

Live PumpSwap FDV via Helius WS; paper wallet + buy/sell with inline PnL.

### Setup

```bash
cd /home/ilyasse/Archetype/archetype-bot
uv sync
```

### Run daemon (migrations / filters / backlog)

```bash
uv run python -m archetype.daemon
```

### CLI

```bash
uv run archetype status
uv run archetype migrations              # RAW unfiltered
uv run archetype migrations --filtered   # PASS only
uv run archetype backlog                 # pending agent queue
uv run archetype watch <MINT>            # live FDV (+ PnL if you hold it)
uv run archetype watch <MINT> --once     # one snapshot

# paper wallet
uv run archetype balance                 # cash + open positions
uv run archetype balance add 50
uv run archetype balance reset           # back to 100 SOL, clear positions
uv run archetype buy <MINT> 5            # paper buy → live watch w/ PnL
uv run archetype buy <MINT> 5 --no-track # fill only
uv run archetype sell <MINT>             # full exit
uv run archetype sell <MINT> 25          # sell 25%
uv run archetype sell <MINT> init        # recover first-buy SOL (fails if underwater)
uv run archetype sell <MINT> 5 sol       # sell 5 SOL worth

uv run archetype stop
```

If you see `address already in use`, an old daemon is still running:

```bash
uv run archetype stop
# or: ss -tlnp | grep 8765
```

See `docs/PROJECT_OVERVIEW.md` for architecture.
