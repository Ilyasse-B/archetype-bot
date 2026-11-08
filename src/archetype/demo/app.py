"""
hackathon viewer control plane on :8787.

does NOT live inside the trading daemon. it only:
  - starts/stops ollama, daemon, agent loop
  - fetches / proxies status + live SSE from the daemon
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from archetype.config import REPO_ROOT, get_settings
from archetype.demo import DEMO_HOST, DEMO_PORT
from archetype.process import already_running, pidfile_path, stop_daemon

log = logging.getLogger("archetype.demo")

_STATIC = REPO_ROOT / "web" / "demo"
_DAEMON_LOG = REPO_ROOT / "data" / "demo_daemon.log"
# keep the log handle alive so the child daemon keeps a valid stdout fd
_daemon_log_handle: Any = None


def create_demo_app() -> FastAPI:
    """build the viewer app. bind only on localhost when serving."""
    app = FastAPI(title="Archetype hackathon viewer", docs_url=None, redoc_url=None)
    settings = get_settings()

    @app.middleware("http")
    async def localhost_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        # process control stays on this machine
        host = (request.client.host if request.client else "") or ""
        if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            return HTMLResponse("localhost only", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        path = _STATIC / "index.html"
        if not path.is_file():
            raise HTTPException(500, "web/demo/index.html missing")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        ollama = await _ollama_up(settings.ollama_base_url)
        daemon = await _daemon_health(settings.base_url) or {}
        loop: dict[str, Any] = {}
        daemon_status: dict[str, Any] = {}
        daemon_up = bool(daemon.get("ok"))
        coin: dict[str, Any] = {}
        live: dict[str, Any] = {}
        if daemon_up:
            loop = await _get_json(f"{settings.base_url}/agent/loop") or {}
            daemon_status = await _get_json(f"{settings.base_url}/status") or {}
            mint = str(loop.get("mint") or "").strip()
            if mint:
                coin = await asyncio.to_thread(
                    _coin_info_for_mint,
                    settings.resolved_trading_ledger_db(),
                    mint,
                )
                # live mcap off the daemon vault socket / peek
                live = await _peek_live(settings.base_url, mint, coin) or {}
        return {
            "viewer": {"host": DEMO_HOST, "port": DEMO_PORT},
            "ollama": {
                "up": ollama,
                "base_url": settings.ollama_base_url,
                "model": settings.ollama_model,
            },
            "daemon": {
                "up": daemon_up,
                "base_url": settings.base_url,
                "detail": daemon,
                "status": daemon_status,
            },
            "loop": loop,
            "coin": coin,
            "live": live,
        }

    @app.post("/api/ollama/start")
    async def ollama_start() -> dict[str, Any]:
        return await _ollama_unit("start", settings.ollama_base_url, settings.ollama_model)

    @app.post("/api/ollama/stop")
    async def ollama_stop() -> dict[str, Any]:
        return await _ollama_unit("stop", settings.ollama_base_url, settings.ollama_model)

    @app.post("/api/daemon/start")
    async def daemon_start() -> dict[str, Any]:
        if await _daemon_health(settings.base_url):
            return {"ok": True, "already": True, "base_url": settings.base_url}
        pid_path = pidfile_path(settings.data_dir)
        existing = already_running(pid_path)
        if existing is not None:
            # pidfile says alive but health failed — still refuse double spawn
            raise HTTPException(
                409,
                f"daemon pidfile alive (pid {existing}) but /health failed. "
                "stop it first.",
            )
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        global _daemon_log_handle  # noqa: PLW0603
        if _daemon_log_handle is None or getattr(_daemon_log_handle, "closed", True):
            _daemon_log_handle = _DAEMON_LOG.open("a", encoding="utf-8")
        try:
            # external process — not a child of the trading app itself
            subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "archetype.daemon"],
                cwd=str(REPO_ROOT),
                stdout=_daemon_log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ},
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"daemon spawn failed: {exc}") from exc
        # wait until health flips or we give up
        deadline = time.time() + 20.0
        while time.time() < deadline:
            await asyncio.sleep(0.4)
            if await _daemon_health(settings.base_url):
                return {"ok": True, "already": False, "base_url": settings.base_url}
        raise HTTPException(
            504,
            f"daemon started but /health not up in time. see {_DAEMON_LOG}",
        )

    @app.post("/api/daemon/stop")
    async def daemon_stop() -> dict[str, Any]:
        # prefer clean http shutdown, then pidfile SIGTERM
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{settings.base_url}/shutdown")
        except Exception:  # noqa: BLE001
            pass
        ok, msg = await asyncio.to_thread(
            stop_daemon, pidfile_path(settings.data_dir), 8.0
        )
        await asyncio.sleep(0.3)
        up = bool(await _daemon_health(settings.base_url))
        return {"ok": ok and not up, "message": msg, "daemon_up": up}

    @app.post("/api/loop/start")
    async def loop_start() -> dict[str, Any]:
        return await _proxy_post(settings.base_url, "/agent/loop/start")

    @app.post("/api/loop/stop")
    async def loop_stop() -> dict[str, Any]:
        return await _proxy_post(settings.base_url, "/agent/loop/stop")

    @app.get("/api/live")
    async def live(filtered: bool = False) -> StreamingResponse:
        if not await _daemon_health(settings.base_url):
            raise HTTPException(503, "daemon not running")
        q = "true" if filtered else "false"
        url = f"{settings.base_url}/live?filtered={q}"

        async def stream() -> AsyncIterator[bytes]:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            yield f"event: error\ndata: daemon live {resp.status_code}\n\n".encode()
                            return
                        async for chunk in resp.aiter_bytes():
                            yield chunk
            except Exception as exc:  # noqa: BLE001
                yield f"event: error\ndata: {exc}\n\n".encode()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    return app


def run(host: str = DEMO_HOST, port: int = DEMO_PORT, open_browser: bool = False) -> None:
    """serve the viewer (blocking)."""
    import uvicorn

    if open_browser:
        import webbrowser

        webbrowser.open(f"http://{host}:{port}/")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log.info("hackathon viewer on http://%s:%s/ (daemon is separate)", host, port)
    uvicorn.run(create_demo_app(), host=host, port=port, log_level="info")


async def _ollama_unit(action: str, base: str, model: str) -> dict[str, Any]:
    """systemctl --user start|stop ollama."""
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["systemctl", "--user", action, "ollama"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"ollama {action} failed: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise HTTPException(500, f"ollama {action} failed: {err}")
    # ollama can take a second to bind after systemctl
    for _ in range(8):
        await asyncio.sleep(0.4)
        up = await _ollama_up(base)
        if action == "stop" and not up:
            break
        if action == "start" and up:
            break
    else:
        up = await _ollama_up(base)
    return {"ok": (not up) if action == "stop" else up, "up": up, "model": model}


async def _ollama_up(base: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{base.rstrip('/')}/api/tags")
            return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _coin_info_for_mint(ledger_path: Path, mint: str) -> dict[str, Any]:
    """latest backlog coin_info for this mint (even if already consumed)."""
    if not mint or not ledger_path.is_file():
        return {}
    try:
        with sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT coin_info FROM migration_backlog "
                "WHERE mint = ? ORDER BY id DESC LIMIT 1",
                (mint,),
            ).fetchone()
    except Exception:  # noqa: BLE001
        return {}
    if not row or not row[0]:
        return {}
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def _peek_live(base: str, mint: str, coin: dict[str, Any]) -> dict[str, Any] | None:
    """POST /agent/peek — fdv/mcap from the live vault socket when focused."""
    body = {
        "mint": mint,
        "known_pool": coin.get("pool_address") or coin.get("pool") or None,
        "name": coin.get("name") or None,
        "symbol": coin.get("symbol") or None,
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(f"{base.rstrip('/')}/agent/peek", json=body)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def _daemon_health(base: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{base.rstrip('/')}/health")
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else {"ok": True}
    except Exception:  # noqa: BLE001
        return None


async def _get_json(url: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def _proxy_post(base: str, path: str) -> dict[str, Any]:
    if not await _daemon_health(base):
        raise HTTPException(503, "daemon not running — start it first")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{base.rstrip('/')}{path}")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"daemon request failed: {exc}") from exc
    if resp.status_code >= 400:
        detail = resp.text
        try:
            detail = str(resp.json().get("detail") or detail)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(resp.status_code, detail)
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"ok": True, "raw": data}
    except Exception:  # noqa: BLE001
        return {"ok": True}
