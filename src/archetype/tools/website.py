"""
website legitimacy + page summary for the focused coin.

focus path expands redirects + gates the url for the coin card / catalogue.
agent path ("tool":"website") does rdap age, urlscans (cached), firecrawl summary.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from archetype.tools.dex import _norm
from archetype.tools.website_cache import urlscans_cache_get, urlscans_cache_put

log = logging.getLogger("archetype.tools.website")

_UA = "Mozilla/5.0 (compatible; ArchetypeBot/0.1; +https://localhost)"
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
}

SOCIAL_HOSTS = frozenset(
    {
        "x.com",
        "twitter.com",
        "t.me",
        "telegram.me",
        "telegram.org",
        "discord.gg",
        "discord.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "instagram.com",
    }
)
LAUNCHPAD_DENY_SUBSTR = ("stonkfun.xyz",)

_URLSCANS = "https://urlscans.com/api/scan"
_FIRECRAWL = "https://api.firecrawl.dev/v2/scrape"
_RDAP_BOOTSTRAP = "https://data.iana.org/rdap/dns.json"

# multi-part public suffixes we bother with in v1 (rest = last 2 labels)
_MULTI_SUFFIX = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "co.jp",
        "com.br",
        "co.in",
        "com.mx",
    }
)

_FOCUS_RESOLVE_TIMEOUT = 4.0
_TOOL_TIMEOUT = 15.0
_SUMMARY_CAP = 1200
_MAX_REDIRECTS = 8

# process-local iana bootstrap
_rdap_bootstrap: list[tuple[str, list[str]]] | None = None

_AGE_NOTE = (
    "young is fine if this is the coin's own page, "
    "suspicious if it claims to be news/blog/external source"
)
_SCORE_NOTE = "soft signal only — take lightly; weigh with age and summary"


@dataclass
class ResolveResult:
    """outcome of a cheap local redirect follow."""

    original_url: str
    final_url: str
    redirected: bool
    error: str | None = None


def website_eligible(coin: dict[str, Any] | None) -> bool:
    """true when catalogue/card should expose the website tool + url."""
    coin = coin if isinstance(coin, dict) else {}
    if "website_gate_ok" in coin:
        return bool(coin.get("website_gate_ok"))
    url = str(coin.get("website") or "").strip()
    return _gates_pass(url, coin)


def showable_website(coin: dict[str, Any] | None) -> str | None:
    """gated website url for the coin card, or None."""
    coin = coin if isinstance(coin, dict) else {}
    if not website_eligible(coin):
        return None
    url = str(coin.get("website") or "").strip()
    return url or None


def resolve_website_url(
    url: str,
    *,
    timeout: float = _FOCUS_RESOLVE_TIMEOUT,
) -> ResolveResult:
    """
    follow http redirects with head, fall back to get.

    no body kept. hop-capped. on failure keep the original url.
    """
    original = _ensure_http_url(url)
    if not original:
        return ResolveResult(
            original_url="",
            final_url="",
            redirected=False,
            error="empty",
        )
    final, err = _follow_redirects(original, timeout=timeout)
    if not final:
        return ResolveResult(
            original_url=original,
            final_url=original,
            redirected=False,
            error=err or "resolve failed",
        )
    return ResolveResult(
        original_url=original,
        final_url=final,
        redirected=_norm(final) != _norm(original),
        error=None,
    )


def prepare_coin_website(
    coin: dict[str, Any] | None,
    *,
    timeout: float = _FOCUS_RESOLVE_TIMEOUT,
) -> dict[str, Any]:
    """
    resolve redirects + gate. mutates a copy of coin for focus / dex merge.

    sets website to final url when resolve works, website_gate_ok, website_resolved.
    keeps website_original when redirected. strips website from socials_present
    when gated out so the card doesnt claim a site we hid.
    """
    out = dict(coin or {})
    raw = str(out.get("website") or "").strip()
    if not raw:
        out["website_resolved"] = True
        out["website_gate_ok"] = False
        return out

    resolved = resolve_website_url(raw, timeout=timeout)
    final = resolved.final_url or raw
    if resolved.redirected:
        out["website_original"] = resolved.original_url
    elif out.get("website_original") and _norm(out.get("website_original")) == _norm(
        final
    ):
        # same as original — drop stale flag
        out.pop("website_original", None)

    out["website"] = final
    out["website_final"] = final
    out["website_resolved"] = True
    # gate on final, and also hide if the *starting* url was already a social/deny
    # (e.g. t.me → telegram.org would otherwise look like a normal site)
    gate_ok = _gates_pass(final, out)
    if gate_ok and _norm(raw) != _norm(final):
        if (
            _hits_deny_list(raw)
            or _is_social_host(_host(raw))
            or _equals_any_social(raw, out)
        ):
            gate_ok = False
    out["website_gate_ok"] = gate_ok
    out["socials_present"] = _adjust_socials_present(
        out.get("socials_present"), gate_ok=gate_ok
    )
    if resolved.error:
        log.debug("website resolve soft-fail %s: %s", raw, resolved.error)
    return out


def fetch_website(
    coin_or_url: dict[str, Any] | str | None,
    *,
    url_scans_key: str = "",
    firecrawl_key: str = "",
    ledger_path: Path | None = None,
    explain: bool = False,
    timeout: float = _TOOL_TIMEOUT,
) -> str:
    """
    agent-facing website block. parallel rdap + urlscans + firecrawl.

    missing pieces are just omitted. total wall clock capped by timeout.
    """
    coin: dict[str, Any]
    if isinstance(coin_or_url, str):
        coin = {"website": coin_or_url, "website_gate_ok": True, "website_resolved": True}
    elif isinstance(coin_or_url, dict):
        coin = coin_or_url
    else:
        return "website: unavailable"

    if not website_eligible(coin):
        return "website: unavailable"

    url = str(coin.get("website") or "").strip()
    if not url:
        return "website: unavailable"

    # focus should already have resolved; only expand if somehow skipped
    original = str(coin.get("website_original") or "").strip() or None
    if not coin.get("website_resolved"):
        prepared = prepare_coin_website(coin)
        if not website_eligible(prepared):
            return "website: unavailable"
        url = str(prepared.get("website") or "").strip()
        original = str(prepared.get("website_original") or "").strip() or None
        if not url:
            return "website: unavailable"

    deadline = time.monotonic() + max(1.0, float(timeout))
    host = _host(url)
    aged_domain = _registrable_domain(host) if host else None

    age_days: int | None = None
    scan: dict[str, Any] | None = None
    summary: str | None = None

    def job_rdap() -> tuple[str, Any]:
        rem = deadline - time.monotonic()
        if rem <= 0.2 or not aged_domain:
            return ("rdap", None)
        return ("rdap", _rdap_domain_age_days(aged_domain, timeout=min(8.0, rem)))

    def job_scan() -> tuple[str, Any]:
        rem = deadline - time.monotonic()
        if rem <= 0.2:
            return ("scan", None)
        return (
            "scan",
            _urlscans_lookup(
                url,
                url_scans_key,
                ledger_path=ledger_path,
                timeout=min(10.0, rem),
            ),
        )

    def job_fc() -> tuple[str, Any]:
        rem = deadline - time.monotonic()
        if rem <= 0.2:
            return ("fc", None)
        return (
            "fc",
            _firecrawl_summary(
                url,
                firecrawl_key,
                timeout=min(12.0, rem),
            ),
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(fn) for fn in (job_rdap, job_scan, job_fc)]
        for fut in as_completed(futs):
            try:
                kind, val = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("website worker failed: %s", exc)
                continue
            if kind == "rdap" and isinstance(val, int):
                age_days = val
            elif kind == "scan" and isinstance(val, dict):
                scan = val
            elif kind == "fc" and isinstance(val, str) and val.strip():
                summary = val.strip()

    result = {
        "final_url": url,
        "original_url": original,
        "domain_age_days": age_days,
        "aged_domain": aged_domain,
        "scan_verdict": (scan or {}).get("verdict"),
        "scan_score": (scan or {}).get("score"),
        "scan_confidence": (scan or {}).get("confidence"),
        "scan_reasons": _reasons_text((scan or {}).get("reasons")),
        "summary": summary,
    }
    return website_text(result, explain=explain)


def website_text(result: dict[str, Any] | None, *, explain: bool = False) -> str:
    """format agent block; drop missing fields silently."""
    data = result if isinstance(result, dict) else {}
    lines: list[str] = []

    final = str(data.get("final_url") or "").strip()
    original = str(data.get("original_url") or "").strip()
    if final:
        if original and _norm(original) != _norm(final):
            if explain:
                lines.append(
                    f"final_url: {final} (redirected from {original})"
                )
            else:
                lines.append(f"final_url: {final} (redirected from {original})")
        else:
            lines.append(f"final_url: {final}")

    age = data.get("domain_age_days")
    aged = str(data.get("aged_domain") or "").strip()
    if isinstance(age, int):
        if explain and aged:
            lines.append(
                f"domain_age_days: {age} (aged {aged}; {_AGE_NOTE})"
            )
        elif explain:
            lines.append(f"domain_age_days: {age} ({_AGE_NOTE})")
        elif aged:
            lines.append(f"domain_age_days: {age} (aged {aged})")
        else:
            lines.append(f"domain_age_days: {age}")

    verdict = data.get("scan_verdict")
    if verdict not in (None, ""):
        lines.append(f"scan_verdict: {verdict}")

    score = data.get("scan_score")
    if score is not None and score != "":
        if explain:
            lines.append(f"scan_score: {score} ({_SCORE_NOTE})")
        else:
            lines.append(f"scan_score: {score}")

    conf = data.get("scan_confidence")
    if conf is not None and conf != "":
        lines.append(f"scan_confidence: {conf}")

    reasons = data.get("scan_reasons")
    if reasons not in (None, ""):
        lines.append(f"scan_reasons: {reasons}")

    summary = str(data.get("summary") or "").strip()
    if summary:
        lines.append(f"summary: {summary}")

    if not lines:
        return "website: unavailable"
    return "website:\n" + "\n".join(lines)


# --- gates / url helpers -------------------------------------------------


def _gates_pass(url: str, coin: dict[str, Any]) -> bool:
    """empty / social mirror / social host / launchpad deny → false."""
    text = (url or "").strip()
    if not text:
        return False
    if _hits_deny_list(text):
        return False
    host = _host(text)
    if host and _is_social_host(host):
        return False
    if _equals_any_social(text, coin):
        return False
    return True


def _hits_deny_list(url: str) -> bool:
    """stonkfun etc — substring on the raw url."""
    low = (url or "").lower()
    return any(part in low for part in LAUNCHPAD_DENY_SUBSTR)


def _is_social_host(host: str) -> bool:
    """known social / platform hosts that are not real project sites."""
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    if h in SOCIAL_HOSTS:
        return True
    # catch discordappcdn-style subdomains lightly
    return any(h == s or h.endswith("." + s) for s in SOCIAL_HOSTS)


def _equals_any_social(url: str, coin: dict[str, Any]) -> bool:
    """website url same as twitter/telegram/other stored socials."""
    target = _norm(url)
    if not target:
        return False
    for key in ("twitter", "telegram"):
        if target == _norm(coin.get(key)):
            return True
    social_kinds = {
        "twitter",
        "x",
        "telegram",
        "discord",
        "youtube",
        "tiktok",
        "instagram",
    }
    for row in coin.get("extra_links") or []:
        if not isinstance(row, dict):
            continue
        other = _norm(row.get("url"))
        if not other or other != target:
            continue
        kind = str(row.get("type") or "").strip().lower()
        if kind in social_kinds or _is_social_host(_host(str(row.get("url") or ""))):
            return True
    return False


def _adjust_socials_present(socials: Any, *, gate_ok: bool) -> list[str]:
    """drop website from the name list when we hide the url."""
    if not isinstance(socials, list):
        socials = []
    out = [str(s) for s in socials if str(s).strip()]
    if gate_ok:
        if "website" not in out:
            out.append("website")
        return out
    return [s for s in out if s != "website"]


def _ensure_http_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    return text


def _host(url: str) -> str:
    text = _ensure_http_url(url)
    if not text:
        return ""
    try:
        host = (urlsplit(text).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _registrable_domain(host: str) -> str:
    """best-effort apex for rdap. multi-part suffix list is tiny on purpose."""
    h = (host or "").lower().strip(".")
    if not h or "." not in h:
        return h
    parts = h.split(".")
    if len(parts) < 2:
        return h
    last2 = ".".join(parts[-2:])
    if last2 in _MULTI_SUFFIX and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def _reasons_text(reasons: Any) -> str | None:
    if reasons is None:
        return None
    if isinstance(reasons, list):
        bits = [str(x).strip() for x in reasons if str(x).strip()]
        return "; ".join(bits) if bits else None
    text = str(reasons).strip()
    return text or None


# --- http redirect follow ------------------------------------------------


def _follow_redirects(url: str, *, timeout: float) -> tuple[str | None, str | None]:
    """head first, then get. returns (final_url, error)."""
    final, err = _follow_with_method(url, "HEAD", timeout=timeout)
    if final:
        return final, None
    # some hosts hate HEAD
    final2, err2 = _follow_with_method(url, "GET", timeout=timeout)
    if final2:
        return final2, None
    return None, err2 or err or "redirect follow failed"


def _follow_with_method(
    url: str,
    method: str,
    *,
    timeout: float,
) -> tuple[str | None, str | None]:
    """manual hop follow so we can cap redirects the same for head/get."""
    current = url
    try:
        with httpx.Client(
            headers={"User-Agent": _UA, "Accept": "*/*"},
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            for _ in range(_MAX_REDIRECTS):
                resp = client.request(method, current)
                # 3xx with Location → next hop
                if resp.status_code in {301, 302, 303, 307, 308}:
                    loc = resp.headers.get("Location")
                    if not loc:
                        return str(resp.url), None
                    current = str(resp.url.join(httpx.URL(loc)))
                    continue
                # method not allowed / not implemented → caller may retry GET
                if method == "HEAD" and resp.status_code in {405, 501}:
                    return None, f"HEAD {resp.status_code}"
                # treat other responses as final (including 4xx/5xx landing)
                return str(resp.url), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    return None, "too many redirects"


# --- rdap ----------------------------------------------------------------


def _rdap_domain_age_days(domain: str, *, timeout: float = 8.0) -> int | None:
    """days since registration via registry rdap. None on miss."""
    domain = (domain or "").strip().lower().strip(".")
    if not domain:
        return None
    endpoints = _rdap_endpoints_for(domain, timeout=timeout)
    if not endpoints:
        return None
    for base in endpoints:
        url = base.rstrip("/") + "/domain/" + domain
        data = _get_json(url, timeout=timeout)
        if not isinstance(data, dict):
            continue
        days = _registration_age_days(data)
        if days is not None:
            return days
    return None


def _rdap_endpoints_for(domain: str, *, timeout: float) -> list[str]:
    """lookup iana bootstrap services for this domain's tld."""
    global _rdap_bootstrap
    if _rdap_bootstrap is None:
        raw = _get_json(_RDAP_BOOTSTRAP, timeout=timeout)
        services: list[tuple[str, list[str]]] = []
        if isinstance(raw, dict):
            for entry in raw.get("services") or []:
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                tlds, urls = entry[0], entry[1]
                if not isinstance(tlds, list) or not isinstance(urls, list):
                    continue
                for tld in tlds:
                    if isinstance(tld, str) and tld:
                        services.append(
                            (
                                tld.lower(),
                                [u for u in urls if isinstance(u, str) and u],
                            )
                        )
        _rdap_bootstrap = services
    labels = domain.split(".")
    # try longest suffix match (co.uk before uk)
    matches: list[str] = []
    for i in range(len(labels)):
        suffix = ".".join(labels[i:])
        for tld, urls in _rdap_bootstrap or []:
            if tld == suffix:
                matches = urls
                break
        if matches:
            break
    return matches


def _registration_age_days(rdap: dict[str, Any]) -> int | None:
    """pull eventAction=registration → age in whole days."""
    events = rdap.get("events")
    if not isinstance(events, list):
        return None
    when = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("eventAction") or "").lower() != "registration":
            continue
        when = ev.get("eventDate")
        if when:
            break
    if not when or not isinstance(when, str):
        return None
    try:
        # rdap dates are usually iso with Z
        ts = when.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0, int((now - dt.astimezone(timezone.utc)).total_seconds() // 86400))
    except Exception:  # noqa: BLE001
        return None


# --- urlscans / firecrawl ------------------------------------------------


def _urlscans_lookup(
    url: str,
    api_key: str,
    *,
    ledger_path: Path | None,
    timeout: float,
) -> dict[str, Any] | None:
    """cache first, then api. returns urlScansAnalysis dict."""
    key = _norm(url) or url.strip()
    hit = urlscans_cache_get(key, ledger_path=ledger_path)
    if isinstance(hit, dict) and hit:
        return hit
    if not (api_key or "").strip():
        return None
    try:
        resp = httpx.get(
            _URLSCANS,
            params={"url": url},
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
                "X-API-Key": api_key.strip(),
            },
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("urlscans failed %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("urlscans %s -> %s", url, resp.status_code)
        return None
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return None
    analysis = payload.get("urlScansAnalysis") if isinstance(payload, dict) else None
    if not isinstance(analysis, dict):
        return None
    urlscans_cache_put(key, analysis, ledger_path=ledger_path)
    return analysis


def _firecrawl_summary(
    url: str,
    api_key: str,
    *,
    timeout: float,
) -> str | None:
    """llm summary only — 1 credit. omit on miss."""
    if not (api_key or "").strip():
        return None
    try:
        resp = httpx.post(
            _FIRECRAWL,
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key.strip()}",
            },
            json={"url": url, "formats": ["summary"]},
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("firecrawl failed %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("firecrawl %s -> %s", url, resp.status_code)
        return None
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    summary = None
    if isinstance(data, dict):
        summary = data.get("summary")
    if not summary and isinstance(payload, dict):
        summary = payload.get("summary")
    text = str(summary or "").strip()
    if not text:
        return None
    if len(text) > _SUMMARY_CAP:
        text = text[: _SUMMARY_CAP - 1].rstrip() + "…"
    return text


def _get_json(url: str, timeout: float) -> Any:
    """quiet json get; None on junk."""
    try:
        resp = httpx.get(
            url,
            headers=_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("website get failed %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("website get %s -> %s", url, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None
