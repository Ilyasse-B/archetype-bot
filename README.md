# Archetype

Local Solana memecoin trading agent (paper-first). Design docs live in `docs/`.

## Phase 3 (in progress)

Live PumpSwap price watch; paper wallet / TP-SL next.

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
uv run archetype watch <MINT>            # live FDV via Helius WS
uv run archetype watch <MINT> --once     # one snapshot
uv run archetype watch <MINT> --poll -i 1  # RPC poll fallback
uv run archetype stop
```

If you see `address already in use`, an old daemon is still running:

```bash
uv run archetype stop
# or: ss -tlnp | grep 8765
```

See `docs/PROJECT_OVERVIEW.md` for architecture.
