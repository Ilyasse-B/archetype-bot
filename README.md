# Archetype

Local Solana memecoin trading agent (paper-first). Design docs live in `docs/`.

## Phase 1 (current)

Scaffold only: `uv` env, FastAPI daemon on localhost, Typer CLI, SQLite stubs.

### Setup

```bash
cd /home/ilyasse/Archetype/archetype-bot
uv sync
```

### Run daemon

```bash
uv run python -m archetype.daemon
```

### CLI

```bash
uv run archetype status
uv run archetype health
uv run archetype version
uv run archetype stop    # clean shutdown — frees the port
```

If you see `address already in use`, an old daemon is still running:

```bash
uv run archetype stop
# or: ss -tlnp | grep 8765
```

See `docs/PROJECT_OVERVIEW.md` for architecture.
