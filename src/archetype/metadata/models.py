"""
parsed off-chain token metadata (metaplex / pump.fun uri json).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenMetadata:
    """one mint's display + socials snapshot."""

    mint: str
    name: str | None = None
    symbol: str | None = None
    description: str | None = None
    image: str | None = None
    video: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    website: str | None = None
    created_on: str | None = None
    show_name: bool | None = None
    coin_community: str | None = None
    json_uri: str | None = None
    uri_fetched_from: str | None = None  # actual gateway/url that worked
    source: str = "none"  # das | uri | das+uri
    raw_uri: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def has_any_social(self) -> bool:
        """true if at least one of twitter/telegram/website is a non-empty string."""
        return bool(self.twitter or self.telegram or self.website)

    def socials_present(self) -> list[str]:
        """which social channels we found."""
        out: list[str] = []
        if self.twitter:
            out.append("twitter")
        if self.telegram:
            out.append("telegram")
        if self.website:
            out.append("website")
        return out

    def to_dict(self) -> dict[str, Any]:
        """json-friendly snapshot for the migration buffer."""
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "description": self.description,
            "image": self.image,
            "video": self.video,
            "twitter": self.twitter,
            "telegram": self.telegram,
            "website": self.website,
            "created_on": self.created_on,
            "show_name": self.show_name,
            "coin_community": self.coin_community,
            "json_uri": self.json_uri,
            "uri_fetched_from": self.uri_fetched_from,
            "source": self.source,
            "socials_present": self.socials_present(),
            "errors": list(self.errors),
        }
