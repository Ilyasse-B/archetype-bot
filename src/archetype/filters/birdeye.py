"""
birdeye global fees — used by the migrate filter gate.

reads global_fees_paid from token_overview. no tracker fallback.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from archetype.config import get_settings

log = logging.getLogger("archetype.birdeye")

_OVERVIEW_URL = "https://public-api.birdeye.so/defi/token_overview"
# free standard is ~1 rps. a couple retries covers migrate bursts.
_MAX_ATTEMPTS = 4
_RETRY_SLEEP_SEC = 1.2
_TIMEOUT_SEC = 20.0


def fetch_global_fees_paid(mint: str) -> float:
    """
    all-time global fees paid for this mint, in SOL.

    raises RuntimeError if the key is missing, the call fails, or the
    field is absent. caller should reject the coin — no backup path.
    """
    key = (get_settings().birdeye_api_key or "").strip()
    if not key:
        raise RuntimeError("ARCHETYPE_BIRDEYE_API_KEY not set")

    last_err = "unknown error"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = httpx.get(
                _OVERVIEW_URL,
                params={"address": mint},
                headers={
                    "X-API-KEY": key,
                    "x-chain": "solana",
                    "accept": "application/json",
                },
                timeout=_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001
            last_err = f"request failed: {exc}"
            log.warning("birdeye overview attempt %s: %s", attempt + 1, last_err)
            time.sleep(_RETRY_SLEEP_SEC)
            continue

        if resp.status_code == 429:
            last_err = "rate limited (429)"
            log.warning("birdeye overview 429 — retrying")
            time.sleep(_RETRY_SLEEP_SEC)
            continue

        if resp.status_code != 200:
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            break

        try:
            body: Any = resp.json()
        except Exception as exc:  # noqa: BLE001
            last_err = f"bad json: {exc}"
            break

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            last_err = f"unexpected body: {str(body)[:200]}"
            break

        raw = data.get("global_fees_paid")
        if raw is None:
            last_err = "global_fees_paid missing from token_overview"
            break
        try:
            return float(raw)
        except (TypeError, ValueError):
            last_err = f"global_fees_paid not a number: {raw!r}"
            break

    raise RuntimeError(f"birdeye global_fees_paid failed for {mint}: {last_err}")
