"""
deterministic off-chain socials gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from archetype.metadata.models import TokenMetadata


@dataclass
class SocialsGateResult:
    """pass/fail for the socials guardrail."""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    socials_present: list[str] = field(default_factory=list)


def apply_socials_gate(meta: TokenMetadata) -> SocialsGateResult:
    """
    settled rule: need at least ONE of twitter / telegram / website.

    no socials at all → reject. any single channel is enough to pass.
    """
    present = meta.socials_present()
    if present:
        return SocialsGateResult(passed=True, socials_present=present)

    reasons: list[str] = []
    if meta.errors:
        # couldn't fetch — still a fail (fail-closed); surface why
        reasons.append("no socials (metadata incomplete: " + "; ".join(meta.errors[:2]) + ")")
    else:
        reasons.append("no socials (need ≥1 of twitter / telegram / website)")
    return SocialsGateResult(passed=False, reasons=reasons, socials_present=[])
