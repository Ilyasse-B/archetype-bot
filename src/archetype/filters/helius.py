"""
thin helius json-rpc client for filter lookups.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger("archetype.helius")


class HeliusRpc:
    """sync httpx client — filters run in a threadpool from async feed."""

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self._api_key = api_key.strip()
        self._url = f"https://mainnet.helius-rpc.com/?api-key={self._api_key}"
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def call(self, method: str, params: Any) -> Any:
        """json-rpc call; raises on http/rpc errors."""
        if not self.enabled:
            raise RuntimeError("ARCHETYPE_HELIUS_API_KEY not set")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        if "error" in body:
            raise RuntimeError(f"helius rpc error: {body['error']}")
        return body.get("result")

    def get_account_info(self, pubkey: str, encoding: str = "base64") -> dict[str, Any] | None:
        """account info or None."""
        result = self.call("getAccountInfo", [pubkey, {"encoding": encoding}])
        return (result or {}).get("value")

    def get_account_json_parsed(self, pubkey: str) -> dict[str, Any] | None:
        """jsonParsed account."""
        return self.get_account_info(pubkey, encoding="jsonParsed")

    def get_transaction(
        self,
        signature: str,
        *,
        retries: int = 8,
        delay_sec: float = 0.4,
    ) -> dict[str, Any] | None:
        """
        confirmed tx — pumpportal can fire before helius has the tx indexed,
        so we retry a bit.
        """
        params = {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed",
        }
        last: dict[str, Any] | None = None
        for attempt in range(max(1, retries)):
            last = self.call("getTransaction", [signature, params])
            if last is not None:
                return last
            if attempt + 1 < retries:
                time.sleep(delay_sec)
        return last

    def get_token_account_balance(self, pubkey: str) -> dict[str, Any] | None:
        """
        token balance value dict, or None if account isn't visible yet.

        helius throws -32602 "could not find account" during migrate races —
        treat that as missing, not a hard crash.
        """
        try:
            result = self.call("getTokenAccountBalance", [pubkey])
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "could not find account" in msg or "-32602" in msg:
                return None
            raise
        return (result or {}).get("value")

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        """largest holders of a mint (empty list if not ready yet)."""
        try:
            result = self.call("getTokenLargestAccounts", [mint])
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "could not find account" in msg or "-32602" in msg:
                return []
            raise
        return (result or {}).get("value") or []

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        """DAS getAsset."""
        return self.call(
            "getAsset",
            {"id": asset_id, "options": {"showFungible": True}},  # type: ignore[arg-type]
        )
