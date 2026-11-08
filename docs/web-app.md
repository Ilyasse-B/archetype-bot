# Hackathon live viewer (web app)

**Not part of the core product.** Separate localhost UI for recording / demos so non-technical viewers can follow migrations and agent thinks without the CLI.

The trading **daemon does not** start or serve this app.

## Quick start

```bash
cd /path/to/archetype-bot
uv sync
uv run archetype demo          # http://127.0.0.1:8787/  (opens browser)
# or:
uv run python -m archetype.demo
```

| Process | Port | Role |
|---------|------|------|
| **Viewer** (this app) | `8787` | Controls + live UI |
| **Daemon** | `8765` (config) | Filters, feeds, agent API, paper |
| **Ollama** | `11434` | Local LLM |

## What it does

1. **Start / stop**
   - Ollama via `systemctl --user start|stop ollama`
   - Daemon as an **external** subprocess: `python -m archetype.daemon` (logs append to `data/demo_daemon.log`)
   - Agent loop via daemon `POST /agent/loop/start|stop`
2. **Status** — polls Ollama, daemon `/health` + `/status`, `/agent/loop`, focused `coin_info` from the ledger, and a live peek (`POST /agent/peek`) for the focused mint so mcap/FDV updates on the coin card
3. **Live** — proxies daemon `GET /live` SSE; agent events drive the left log; filter PASSes flash there; filter rejects stay muted (one card per mint); migrations column is hidden (coin card is the focus)

## Layout

- Header: logo (`web/demo/logo.png`) + “Archetype live” + control groups (ollama / daemon / agent)
- Thin status strip (phase / action / narrative / backlog / model / mint) — ellipsis overflow with a CSS hover tip for the full text (native `title` was unreliable)
- Left (~48%): agent log — decision rows titled by the real action (`wait` / `buy` / `reject` / …); finished mint collapsed into one archive block; hover expands full tool/decision text
- Center: current coin card (name, ticker, mint, venue, **live** mcap from peek, fees, socials, …)

UI is intentionally plain — ops console for screen recording.

## Code map

| Path | Role |
|------|------|
| [`src/archetype/demo/app.py`](../src/archetype/demo/app.py) | FastAPI control plane + SSE proxy + peek for live mcap |
| [`src/archetype/demo/__init__.py`](../src/archetype/demo/__init__.py) | `DEMO_HOST` / `DEMO_PORT` |
| [`src/archetype/demo/__main__.py`](../src/archetype/demo/__main__.py) | `python -m archetype.demo` |
| [`web/demo/index.html`](../web/demo/index.html) | Single-page UI (HTML/CSS/JS) |
| [`web/demo/logo.png`](../web/demo/logo.png) | Header logo |
| [`src/archetype/cli.py`](../src/archetype/cli.py) `demo` | CLI entry `archetype demo` |

### Viewer HTTP API

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/` | Serves `web/demo/index.html` |
| `GET` | `/static/logo.png` | Header logo |
| `GET` | `/api/status` | Aggregated ollama + daemon + loop + `live` peek fields |
| `POST` | `/api/ollama/start` \| `/api/ollama/stop` | User systemd unit |
| `POST` | `/api/daemon/start` | Spawn daemon if down |
| `POST` | `/api/daemon/stop` | Daemon `/shutdown` + pidfile stop |
| `POST` | `/api/loop/start` \| `/api/loop/stop` | Forward to daemon |
| `GET` | `/api/live?filtered=` | Proxy daemon SSE |

All of the above are **localhost-only** (middleware rejects non-loopback clients). API keys are never sent to the browser.

## Data flow

```text
Browser  →  Viewer :8787  →  Daemon :8765  (/health, /status, /agent/loop, /agent/peek, /live, /shutdown)
                         →  Ollama :11434 (/api/tags) + systemctl start
                         →  subprocess: python -m archetype.daemon
```

Live UI uses `EventSource("/api/live")`. The viewer streams bytes from the daemon so the browser stays same-origin (no CORS fight). Status polling also calls `/agent/peek` for the focused mint so the coin card mcap stays live.

## Changing things later

- **UI copy / layout** → edit `web/demo/index.html` only (no build step).
- **New control** → add a route in `demo/app.py`, button + `fetch` in the HTML.
- **Event rendering** → `connectLive()` in the HTML; kinds mirror `src/archetype/agent/live.py`.
- **Port** → `DEMO_PORT` in `demo/__init__.py`, or `archetype demo --port …`.

## What this is not

- Not served by `archetype.daemon`
- Not required to run the agent day-to-day (CLI + daemon is enough)
- Not a polished product dashboard
