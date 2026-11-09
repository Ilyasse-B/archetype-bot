"""
vamp / copycat scanner.

deterministic same-narrative matcher + in-memory buffer.
not agent-callable — the filter hook / loop feeds alerts.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from archetype.filters.thresholds import WSOL_MINT

log = logging.getLogger("archetype.vamp")

# how many buffered hits we keep while searching
_BUFFER_CAP = 10

# lone tokens that dont mean much for name overlap
_NAME_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "coin",
        "token",
        "inu",
        "ai",
        "sol",
        "solana",
        "meme",
        "memecoin",
    }
)

_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)

_NOTE_NAME_SYMBOL = (
    "compare the name and symbol of this coin to your current coin's "
    "name and symbol, are they better?"
)
_NOTE_MATCHED = "these are the fields that matched to your current coin"
_NOTE_PAIRING = (
    "evaluate if this pairing is better than the pairing of your current "
    "coin (if it has one) in relation to the narrative"
)


@dataclass
class VampHit:
    """one deterministic match against the focused coin."""

    mint: str
    name: str | None
    symbol: str | None
    matched_on: list[str] = field(default_factory=list)
    quote_label: str | None = None
    why: str = ""

    def to_payload(self, *, text: str) -> dict[str, Any]:
        """what we store on focus memory / live feed (mint kept for dismiss)."""
        return {
            "text": text,
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "matched_on": list(self.matched_on),
            "quote_label": self.quote_label,
            "why": self.why,
        }


def norm_text(value: Any) -> str:
    """lowercase, strip spaces + punctuation. empty if nothing left."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if not text:
        return ""
    return _PUNCT_RE.sub("", text)


def norm_ticker(value: Any) -> str:
    """same as norm_text — ticker compare uses the compact string."""
    return norm_text(value)


def norm_url(url: Any) -> str:
    """same link, different spelling. twitter.com and x.com match."""
    if not isinstance(url, str):
        return ""
    text = url.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "twitter.com":
        host = "x.com"
    path = parts.path.rstrip("/").lower()
    return urlunsplit(("https", host, path, "", ""))


def _name_tokens(value: Any) -> list[str]:
    """split a display name into tokens, drop weak stopwords."""
    if not isinstance(value, str):
        return []
    raw = re.findall(r"[a-z0-9]+", value.strip().lower())
    out = [tok for tok in raw if tok and tok not in _NAME_STOP]
    return out


def _ticker_hit(focus_sym: str, cand_sym: str) -> bool:
    """exact always; containment only when both length >= 3."""
    if not focus_sym or not cand_sym:
        return False
    if focus_sym == cand_sym:
        return True
    # short tickers (1-2) are exact-only — AI must not match AIR
    if len(focus_sym) < 3 or len(cand_sym) < 3:
        return False
    return focus_sym in cand_sym or cand_sym in focus_sym


def _name_hit(focus_name: Any, cand_name: Any) -> bool:
    """exact compact match, or strong token overlap / containment."""
    f_compact = norm_text(focus_name)
    c_compact = norm_text(cand_name)
    if f_compact and c_compact and f_compact == c_compact:
        return True
    f_toks = _name_tokens(focus_name)
    c_toks = _name_tokens(cand_name)
    if not f_toks or not c_toks:
        return False
    # multi-token containment of the whole distinctive phrase
    if len(f_toks) >= 2 and " ".join(f_toks) in " ".join(c_toks):
        return True
    if len(c_toks) >= 2 and " ".join(c_toks) in " ".join(f_toks):
        return True
    shared = set(f_toks) & set(c_toks)
    if not shared:
        return False
    # need at least one distinctive shared token length >= 3,
    # and either majority overlap or 2+ shared tokens
    distinctive = {t for t in shared if len(t) >= 3}
    if not distinctive:
        return False
    if len(shared) >= 2:
        return True
    smaller = min(len(set(f_toks)), len(set(c_toks)))
    return smaller > 0 and len(shared) / smaller >= 0.67


def _social_matched(focus: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """shared normalized social urls → matched_on keys only."""
    matched: list[str] = []
    for key in ("twitter", "telegram", "website"):
        f_url = norm_url(focus.get(key))
        c_url = norm_url(candidate.get(key))
        if not f_url or not c_url:
            continue
        if f_url == c_url:
            matched.append(key)
            continue
        # status id match across twitter/website slots (x.com/.../status/id)
        f_status = _status_id(f_url)
        c_status = _status_id(c_url)
        if f_status and c_status and f_status == c_status:
            matched.append(key)
    return matched


def _status_id(normed: str) -> str | None:
    """pull /status/<id> from a normalized x.com url."""
    if "/status/" not in normed:
        return None
    tail = normed.split("/status/", 1)[1]
    sid = tail.split("/", 1)[0].strip()
    return sid or None


def _quote_label(coin: dict[str, Any]) -> str | None:
    """show a pairing label when the candidate is not plain SOL."""
    sym = (coin.get("quote_symbol") or "").strip()
    if sym:
        return sym
    qm = str(coin.get("quote_mint") or "").strip()
    if not qm or qm == WSOL_MINT:
        return None
    return qm[:8] + "…"


def _said(label: str, value: str, note: str, explain: bool) -> str:
    """number/value on its own, or value plus a short note the first time."""
    if not explain:
        return f"{label}: {value}"
    return f"{label}: {value} ({note})"


def match_vamp(
    focus: dict[str, Any] | None, candidate: dict[str, Any] | None
) -> VampHit | None:
    """
    deterministic same-narrative check.

    any strong name/ticker/social hit → VampHit.
    pairing is shown for the agent to judge — we do not score “stronger”.
    """
    if not isinstance(focus, dict) or not isinstance(candidate, dict):
        return None
    focus_mint = str(focus.get("mint") or focus.get("coin_mint") or "").strip()
    cand_mint = str(candidate.get("mint") or candidate.get("coin_mint") or "").strip()
    if not cand_mint:
        return None
    if focus_mint and cand_mint == focus_mint:
        return None

    matched: list[str] = []
    f_sym = norm_ticker(focus.get("symbol"))
    c_sym = norm_ticker(candidate.get("symbol"))
    if _ticker_hit(f_sym, c_sym):
        matched.append("ticker")

    if _name_hit(focus.get("name"), candidate.get("name")):
        matched.append("name")

    for key in _social_matched(focus, candidate):
        if key not in matched:
            matched.append(key)

    if not matched:
        return None

    why = "matched on " + ", ".join(matched)

    hit = VampHit(
        mint=cand_mint,
        name=(str(candidate.get("name")).strip() if candidate.get("name") else None),
        symbol=(
            str(candidate.get("symbol")).strip() if candidate.get("symbol") else None
        ),
        matched_on=matched,
        quote_label=_quote_label(candidate),
        why=why,
    )
    log.info(
        "VAMP MATCH focus=%s cand=%s name=%s sym=%s on=%s quote=%s",
        focus_mint[:8] if focus_mint else "-",
        cand_mint[:8],
        (hit.name or "?")[:24],
        hit.symbol or "?",
        ",".join(matched),
        hit.quote_label or "-",
    )
    return hit


def format_vamp_alert(
    hit: VampHit,
    *,
    holding: bool = True,
    explain: bool = True,
) -> str:
    """
    agent-facing block. one hit per block.

    holding=True → “selling”; False → “rejecting” (not bought yet).
    mint stays off this text — only in the stored payload for dismiss.
    """
    action = "selling" if holding else "rejecting"
    lines = [
        "Vamp alert - another coin has migrated which may be similar to the "
        "coin you are currently focused on, assess the below details and "
        f"decide whether it is worth {action} your current coin"
    ]
    name = hit.name or "?"
    sym = hit.symbol or "?"
    lines.append(
        _said(
            "name",
            f"{name}  symbol: {sym}",
            _NOTE_NAME_SYMBOL,
            explain,
        )
    )
    if hit.matched_on:
        lines.append(
            _said(
                "matched",
                ", ".join(hit.matched_on),
                _NOTE_MATCHED,
                explain,
            )
        )
    if hit.quote_label:
        lines.append(
            _said(
                "pairing",
                hit.quote_label,
                _NOTE_PAIRING,
                explain,
            )
        )
    lines.append(
        "in your json reply, fill reasoning with why you sell, hold, reject, "
        "or otherwise keep this focus after this vamp alert"
    )
    return "\n".join(lines)


class VampBuffer:
    """
    in-memory vamp backlog for the current focus.

    searching → buffer hits. buy → flush. reject/drop/idle → clear.
    locked alerts go straight to the loop (not stored here long-term).
    """

    def __init__(self, *, cap: int = _BUFFER_CAP) -> None:
        self._cap = max(1, int(cap))
        self._lock = threading.Lock()
        self.focus_mint: str | None = None
        self._hits: dict[str, VampHit] = {}
        self._order: list[str] = []
        # mints we already showed the agent this focus (for dismiss-on-hold)
        self._presented: set[str] = set()

    def set_focus(self, mint: str | None) -> None:
        """tie buffer to this focus mint; changing focus clears."""
        mint = (mint or "").strip() or None
        with self._lock:
            if mint == self.focus_mint:
                return
            self.focus_mint = mint
            self._hits.clear()
            self._order.clear()
            self._presented.clear()
            log.info("VAMP buffer focus → %s", mint or "idle")

    def clear(self) -> None:
        """drop every buffered hit (reject / drop / idle / after flush)."""
        with self._lock:
            n = len(self._hits)
            self._hits.clear()
            self._order.clear()
            self._presented.clear()
        if n:
            log.info("VAMP buffer cleared (%s hits)", n)

    def note_presented(self, mint: str) -> None:
        """remember we already alerted this vamp mint on the current focus."""
        mint = (mint or "").strip()
        if not mint:
            return
        with self._lock:
            self._presented.add(mint)

    def take_presented(self) -> list[str]:
        """mints shown this focus, then forget them (used on hold dismiss)."""
        with self._lock:
            out = list(self._presented)
            self._presented.clear()
        return out

    def buffer_add(self, hit: VampHit) -> None:
        """dedupe by mint; drop oldest when over cap."""
        if not hit.mint:
            return
        with self._lock:
            if hit.mint in self._hits:
                self._hits[hit.mint] = hit
                log.info("VAMP buffer update mint=%s", hit.mint[:8])
                return
            self._hits[hit.mint] = hit
            self._order.append(hit.mint)
            while len(self._order) > self._cap:
                old = self._order.pop(0)
                self._hits.pop(old, None)
                log.info("VAMP buffer overflow drop mint=%s", old[:8])
        log.info(
            "VAMP buffer +%s (%s) size=%s",
            hit.mint[:8],
            ",".join(hit.matched_on),
            len(self._order),
        )

    def flush(self) -> list[VampHit]:
        """take buffered hits newest-first, then clear."""
        with self._lock:
            ordered = list(reversed(self._order))
            hits = [self._hits[m] for m in ordered if m in self._hits]
            self._hits.clear()
            self._order.clear()
        if hits:
            log.info("VAMP flush %s hits", len(hits))
        return hits

    def peek(self) -> list[VampHit]:
        """copy of buffered hits newest-first (no clear)."""
        with self._lock:
            ordered = list(reversed(self._order))
            return [self._hits[m] for m in ordered if m in self._hits]

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)
