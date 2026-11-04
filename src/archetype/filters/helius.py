"""
thin helius json-rpc client for filter lookups + price.

keeps a persistent httpx client (connection reuse) and supports
getMultipleAccounts batching.
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
        self._client: httpx.Client | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def ws_url(self) -> str:
        """helius websocket endpoint."""
        return f"wss://mainnet.helius-rpc.com/?api-key={self._api_key}"

    def _http(self) -> httpx.Client:
        """lazy persistent client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        """drop the http client (optional cleanup)."""
        if self._client is not None and not self._client.is_closed:
            self._client.close()
        self._client = None

    def call(self, method: str, params: Any) -> Any:
        """json-rpc call; raises on http/rpc errors."""
        if not self.enabled:
            raise RuntimeError("ARCHETYPE_HELIUS_API_KEY not set")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = self._http().post(self._url, json=payload)
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

    def get_multiple_accounts(
        self,
        pubkeys: list[str],
        *,
        encoding: str = "base64",
    ) -> list[dict[str, Any] | None]:
        """
        batch getAccountInfo — one RPC for many keys.

        returns a list aligned with pubkeys (None if missing).
        """
        if not pubkeys:
            return []
        out: list[dict[str, Any] | None] = []
        chunk_size = 100
        for i in range(0, len(pubkeys), chunk_size):
            chunk = pubkeys[i : i + chunk_size]
            result = self.call(
                "getMultipleAccounts",
                [chunk, {"encoding": encoding}],
            )
            values = (result or {}).get("value") or []
            while len(values) < len(chunk):
                values.append(None)
            out.extend(values[: len(chunk)])
        return out

    def get_transaction(
        self,
        signature: str,
        *,
        retries: int = 6,
        delay_sec: float = 0.35,
    ) -> dict[str, Any] | None:
        """
        confirmed tx — pumpportal can fire before helius has the tx indexed,
        so we retry a bit (backoff).
        """
        params = {
            "encoding": "jsonParsed",
            # v0 txs (and migrate_v2) need this ≥ 0; 1 is the safe default
            "maxSupportedTransactionVersion": 1,
            "commitment": "confirmed",
        }
        last: dict[str, Any] | None = None
        delay = delay_sec
        for attempt in range(max(1, retries)):
            last = self.call("getTransaction", [signature, params])
            if last is not None:
                return last
            if attempt + 1 < retries:
                time.sleep(delay)
                delay = min(delay * 1.5, 2.0)
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

    def get_token_balances_pair(
        self, base_ata: str, quote_ata: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """
        fetch two vault balances in one getMultipleAccounts (jsonParsed).

        falls back to per-account calls if parsed shape is weird.
        """
        try:
            values = self.get_multiple_accounts(
                [base_ata, quote_ata], encoding="jsonParsed"
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("batch vault balances failed, falling back: %s", exc)
            return (
                self.get_token_account_balance(base_ata),
                self.get_token_account_balance(quote_ata),
            )

        def _from_acct(acct: dict[str, Any] | None) -> dict[str, Any] | None:
            if not acct:
                return None
            data = acct.get("data")
            if not isinstance(data, dict):
                return None
            parsed = data.get("parsed") or {}
            info = parsed.get("info") or {}
            ta = info.get("tokenAmount")
            return ta if isinstance(ta, dict) else None

        base = _from_acct(values[0] if values else None)
        quote = _from_acct(values[1] if len(values) > 1 else None)
        if base is None:
            base = self.get_token_account_balance(base_ata)
        if quote is None:
            quote = self.get_token_account_balance(quote_ata)
        return base, quote

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
