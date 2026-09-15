"""
fetch pump / metaplex metadata via helius DAS + off-chain json uri.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from archetype.filters.helius import HeliusRpc
from archetype.metadata.models import TokenMetadata

log = logging.getLogger("archetype.metadata")

# ipfs.io / public gateways rate-limit a lot; pinata has been reliable for us
_IPFS_GATEWAYS = (
    "https://gateway.pinata.cloud/ipfs/{cid}",
    "https://nftstorage.link/ipfs/{cid}",
    "https://dweb.link/ipfs/{cid}",
    "https://ipfs.io/ipfs/{cid}",
)

_UA = "Mozilla/5.0 (compatible; ArchetypeBot/0.1; +https://localhost)"


def _clean_str(value: Any) -> str | None:
    """trim empty / nullish strings."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value or value.lower() in {"null", "none", "n/a", "-"}:
        return None
    return value


def _ipfs_cid(uri: str) -> str | None:
    """pull cid out of ipfs:// or .../ipfs/<cid> urls."""
    u = uri.strip()
    if u.startswith("ipfs://"):
        return u[len("ipfs://") :].lstrip("/").split("?")[0] or None
    m = re.search(r"/ipfs/([^/?#]+)", u)
    return m.group(1) if m else None


def _candidate_urls(json_uri: str) -> list[str]:
    """gateway mirrors first for ipfs (ipfs.io 403s a lot), then original uri."""
    uri = json_uri.strip()
    out: list[str] = []
    cid = _ipfs_cid(uri)
    if cid:
        for tmpl in _IPFS_GATEWAYS:
            alt = tmpl.format(cid=cid)
            if alt not in out:
                out.append(alt)
    if uri and uri not in out:
        out.append(uri)
    return out


def _pick_link(data: dict[str, Any], *keys: str) -> str | None:
    """first non-empty string among keys (and nested socials dict if present)."""
    for k in keys:
        v = _clean_str(data.get(k))
        if v:
            return v
    socials = data.get("socials")
    if isinstance(socials, dict):
        for k in keys:
            v = _clean_str(socials.get(k))
            if v:
                return v
    return None


def _looks_like_url(value: str) -> bool:
    """loose check — pump metadata sometimes stores handles without scheme."""
    v = value.strip()
    if v.startswith(("http://", "https://", "ipfs://")):
        return True
    # bare handles / domains traders still treat as "has a social"
    if v.startswith("@") or "." in v or v.startswith("t.me/"):
        return True
    return False


def _normalize_social(value: str | None, kind: str) -> str | None:
    """keep as-is if url-ish; drop garbage."""
    if not value:
        return None
    if not _looks_like_url(value):
        return None
    # if it's a bare twitter handle, make it a link so later tools are happier
    if kind == "twitter" and value.startswith("@") and "://" not in value:
        return f"https://x.com/{value.lstrip('@')}"
    if kind == "twitter" and re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
        return f"https://x.com/{value}"
    if kind == "telegram" and value.startswith("t.me/"):
        return f"https://{value}"
    if kind == "website" and "://" not in value and "." in value:
        return f"https://{value}"
    return value


def fetch_token_metadata(mint: str, rpc: HeliusRpc, timeout: float = 20.0) -> TokenMetadata:
    """
    DAS getAsset for name/symbol/uri, then HTTP GET the uri json for socials.

    fails soft into `.errors` — caller decides pass/fail via socials gate.
    """
    meta = TokenMetadata(mint=mint)

    # --- DAS ---
    asset: dict[str, Any] | None = None
    try:
        asset = rpc.get_asset(mint)
    except Exception as exc:  # noqa: BLE001
        meta.errors.append(f"das getAsset failed: {exc}")
        log.warning("das getAsset failed for %s: %s", mint, exc)

    if asset:
        content = asset.get("content") or {}
        m = content.get("metadata") or {}
        links = content.get("links") or {}
        files = content.get("files") or []

        meta.name = _clean_str(m.get("name"))
        meta.symbol = _clean_str(m.get("symbol"))
        meta.description = _clean_str(m.get("description"))
        meta.json_uri = _clean_str(content.get("json_uri"))
        meta.image = _clean_str(links.get("image")) if isinstance(links, dict) else None
        if not meta.image and files and isinstance(files[0], dict):
            meta.image = _clean_str(files[0].get("cdn_uri") or files[0].get("uri"))
        # das sometimes already has external urls under links
        if isinstance(links, dict):
            meta.twitter = _normalize_social(_clean_str(links.get("twitter")), "twitter")
            meta.telegram = _normalize_social(_clean_str(links.get("telegram")), "telegram")
            meta.website = _normalize_social(
                _clean_str(links.get("website") or links.get("external_url")),
                "website",
            )
        meta.source = "das"

        # fallback uri from token-2022 extension if das missed it
        if not meta.json_uri:
            try:
                acct = rpc.get_account_json_parsed(mint)
                info = ((acct or {}).get("data") or {}).get("parsed") or {}
                info = info.get("info") or {}
                for ext in info.get("extensions") or []:
                    if ext.get("extension") == "tokenMetadata":
                        state = ext.get("state") or {}
                        meta.json_uri = _clean_str(state.get("uri"))
                        meta.name = meta.name or _clean_str(state.get("name"))
                        meta.symbol = meta.symbol or _clean_str(state.get("symbol"))
                        break
            except Exception as exc:  # noqa: BLE001
                meta.errors.append(f"mint extension uri fallback failed: {exc}")

    # --- URI json (where pump puts twitter/telegram/website) ---
    if not meta.json_uri:
        meta.errors.append("no metadata json_uri")
        return meta

    uri_data = _fetch_uri_json(meta.json_uri, timeout=timeout)
    if uri_data is None:
        meta.errors.append(f"uri fetch failed for {meta.json_uri}")
        return meta

    fetched_from, data = uri_data
    meta.uri_fetched_from = fetched_from
    meta.raw_uri = data
    meta.source = "das+uri" if meta.source == "das" else "uri"

    meta.name = _clean_str(data.get("name")) or meta.name
    meta.symbol = _clean_str(data.get("symbol")) or meta.symbol
    meta.description = _clean_str(data.get("description")) or meta.description
    meta.image = _clean_str(data.get("image")) or meta.image
    meta.video = _clean_str(data.get("video"))
    meta.created_on = _clean_str(data.get("createdOn") or data.get("created_on"))
    show = data.get("showName")
    if isinstance(show, bool):
        meta.show_name = show
    meta.coin_community = _clean_str(data.get("coin_community") or data.get("community"))

    meta.twitter = _normalize_social(
        _pick_link(data, "twitter", "Twitter", "x", "X"),
        "twitter",
    ) or meta.twitter
    meta.telegram = _normalize_social(
        _pick_link(data, "telegram", "Telegram", "tg"),
        "telegram",
    ) or meta.telegram
    meta.website = _normalize_social(
        _pick_link(data, "website", "Website", "web", "external_url", "externalUrl"),
        "website",
    ) or meta.website

    return meta


def _fetch_uri_json(json_uri: str, timeout: float = 20.0) -> tuple[str, dict[str, Any]] | None:
    """try original + gateway mirrors until one returns json."""
    headers = {"User-Agent": _UA, "Accept": "application/json,*/*"}
    last_err: str | None = None
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for url in _candidate_urls(json_uri):
            try:
                resp = client.get(url)
                if resp.status_code != 200:
                    last_err = f"{urlparse(url).netloc}:{resp.status_code}"
                    continue
                data = resp.json()
                if not isinstance(data, dict):
                    last_err = f"{urlparse(url).netloc}:not-object"
                    continue
                return url, data
            except Exception as exc:  # noqa: BLE001
                last_err = f"{urlparse(url).netloc}:{exc}"
                continue
    log.warning("all uri gateways failed for %s (%s)", json_uri, last_err)
    return None
