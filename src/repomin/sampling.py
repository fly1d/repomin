"""Shared validation for baseline and candidate sampling policies."""

from __future__ import annotations

from typing import Optional

from repomin.oracle import clopper_pearson_lower_bound, exact_binomial_rate_gate


def sample_threshold(
    runs: int,
    minimum: Optional[int],
    label: str,
    minimum_rate: Optional[float] = None,
) -> int:
    """Resolve a count threshold without overriding an explicit rate policy."""
    if runs < 1:
        raise ValueError("%s runs must be at least 1" % label)
    # A rate criterion supplies the statistical requirement. Requiring every
    # sample as well would make a flaky-failure mode equivalent to strict mode.
    required = (1 if minimum_rate is not None else runs) if minimum is None else minimum
    if required < 1 or required > runs:
        raise ValueError(
            "minimum %s passes must be between 1 and %s runs" % (label, label)
        )
    return required


def validate_rate_attainable(
    runs: int,
    minimum_rate: Optional[float],
    confidence: float,
    label: str,
    signature_discovery: bool = False,
) -> None:
    """Reject a rate gate that cannot pass even when every sample succeeds."""
    if minimum_rate is None:
        return
    evidence_runs = runs - int(signature_discovery)
    if exact_binomial_rate_gate(
        evidence_runs,
        evidence_runs,
        minimum_rate,
        confidence,
    ):
        return
    best_possible = clopper_pearson_lower_bound(
        evidence_runs,
        evidence_runs,
        confidence,
    )
    discovery_detail = (
        ", leaving %d post-discovery rate-evidence runs" % evidence_runs
        if signature_discovery
        else ""
    )
    raise ValueError(
        "minimum %s rate %.4g is unattainable with %d %s runs%s at %.4g "
        "confidence (best possible exact lower bound: %.4g); increase "
        "--%s-runs or lower the rate/confidence"
        % (
            label,
            minimum_rate,
            runs,
            label,
            discovery_detail,
            confidence,
            best_possible,
            label,
        )
    )
