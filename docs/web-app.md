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
   - Ollama via `systemctl --user start ollama`
   - Daemon as an **external** subprocess: `python -m archetype.daemon` (logs append to `data/demo_daemon.log`)
   - Agent loop via daemon `POST /agent/loop/start|stop`
2. **Status** — polls Ollama, daemon `/health` + `/status`, `/agent/loop`
3. **Live tails** — proxies daemon `GET /live` SSE into the page (migrations + agent events)

## Layout

- Top: control groups (ollama / daemon / agent) with status pills
- Focus strip: phase, mint, last action, narrative, backlog count, model
- Left column: migrations (pass/fail)
- Right column: agent events (thinking, buy, reject, tools, …)

UI is intentionally plain — ops console for screen recording.

## Code map

| Path | Role |
|------|------|
| [`src/archetype/demo/app.py`](../src/archetype/demo/app.py) | FastAPI control plane + SSE proxy |
| [`src/archetype/demo/__init__.py`](../src/archetype/demo/__init__.py) | `DEMO_HOST` / `DEMO_PORT` |
| [`src/archetype/demo/__main__.py`](../src/archetype/demo/__main__.py) | `python -m archetype.demo` |
| [`web/demo/index.html`](../web/demo/index.html) | Single-page UI (HTML/CSS/JS) |
| [`src/archetype/cli.py`](../src/archetype/cli.py) `demo` | CLI entry `archetype demo` |

### Viewer HTTP API

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/` | Serves `web/demo/index.html` |
| `GET` | `/api/status` | Aggregated ollama + daemon + loop |
| `POST` | `/api/ollama/start` | User systemd unit |
| `POST` | `/api/daemon/start` | Spawn daemon if down |
| `POST` | `/api/daemon/stop` | Daemon `/shutdown` + pidfile stop |
| `POST` | `/api/loop/start` \| `/api/loop/stop` | Forward to daemon |
| `GET` | `/api/live?filtered=` | Proxy daemon SSE |

All of the above are **localhost-only** (middleware rejects non-loopback clients). API keys are never sent to the browser.

## Data flow

```text
Browser  →  Viewer :8787  →  Daemon :8765  (/health, /status, /agent/loop, /live, /shutdown)
                         →  Ollama :11434 (/api/tags) + systemctl start
                         →  subprocess: python -m archetype.daemon
```

Live UI uses `EventSource("/api/live")`. The viewer streams bytes from the daemon so the browser stays same-origin (no CORS fight).

## Changing things later

- **UI copy / layout** → edit `web/demo/index.html` only (no build step).
- **New control** → add a route in `demo/app.py`, button + `fetch` in the HTML.
- **Event rendering** → `connectLive()` in the HTML; kinds mirror `src/archetype/agent/live.py`.
- **Port** → `DEMO_PORT` in `demo/__init__.py`, or `archetype demo --port …`.

## What this is not

- Not served by `archetype.daemon`
- Not required to run the agent day-to-day (CLI + daemon is enough)
- Not a polished product dashboard
