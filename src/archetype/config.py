"""
app settings — loads yaml + env vars.

env wins over yaml for secrets / overrides.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo root is three levels up from this file: src/archetype/config.py
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """read yaml if it exists, else empty dict."""
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


class Settings(BaseSettings):
    """runtime config for daemon + cli."""

    model_config = SettingsConfigDict(
        env_prefix="ARCHETYPE_",
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # api / daemon
    host: str = "127.0.0.1"
    port: int = 8765
    agent_state: str = "idle"  # idle | searching | locked — stub for now

    # paths
    data_dir: Path = Field(default_factory=lambda: REPO_ROOT / "data")
    active_context_db: Path | None = None
    trading_ledger_db: Path | None = None

    # placeholders for later phases (keys from env)
    helius_api_key: str = ""
    pumpportal_api_key: str = ""

    # local llm (phase 4) — one model, one call at a time
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout_sec: float = 30.0
    # lock loop task — off so the human trade stream keeps the backlog
    agent_loop_enabled: bool = False

    def resolved_active_context_db(self) -> Path:
        """path to the sliding-window context db."""
        if self.active_context_db is not None:
            return self.active_context_db
        return self.data_dir / "active_context.db"

    def resolved_trading_ledger_db(self) -> Path:
        """path to the permanent ledger db."""
        if self.trading_ledger_db is not None:
            return self.trading_ledger_db
        return self.data_dir / "trading_ledger.db"

    @property
    def base_url(self) -> str:
        """localhost fastapi base url for the cli."""
        return f"http://{self.host}:{self.port}"


@lru_cache
def get_settings() -> Settings:
    """
    load default.yaml then overlay env.

    cached so we dont re-read files every call.
    """
    yaml_data = _load_yaml(DEFAULT_CONFIG_PATH)
    # flatten a bit — yaml may nest under keys we care about
    flat: dict[str, Any] = {}
    for key in (
        "host",
        "port",
        "agent_state",
        "data_dir",
        "active_context_db",
        "trading_ledger_db",
        "helius_api_key",
        "pumpportal_api_key",
        "ollama_base_url",
        "ollama_model",
        "ollama_timeout_sec",
        "agent_loop_enabled",
    ):
        if key in yaml_data:
            flat[key] = yaml_data[key]
    # nested api block support
    api = yaml_data.get("api") or {}
    if isinstance(api, dict):
        if "host" in api:
            flat["host"] = api["host"]
        if "port" in api:
            flat["port"] = api["port"]

    return Settings(**flat)
