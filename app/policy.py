"""Deterministic policy gate.

The policy is supplied by the team running the demo; this service verifies a
plan against that policy. It does not certify legal compliance of any kind.
A region that fails the gate is excluded from recommendations, no matter how
low its carbon intensity is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.carbon import CarbonCatalog


@dataclass
class Policy:
    name: str
    allowed_regions: list[str]
    residency_jurisdiction: str | None = None  # ISO country code, e.g. "JP"
    residency_label: str | None = None  # human label, e.g. "Japan"
    description: str | None = None

    @staticmethod
    def from_dict(raw: object) -> "Policy":
        if not isinstance(raw, dict):
            raise ValueError("Policy must be a JSON object.")
        name = raw.get("name")
        allowed = raw.get("allowed_regions")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Policy needs a non-empty 'name'.")
        if not isinstance(allowed, list) or not all(isinstance(r, str) for r in allowed):
            raise ValueError("Policy needs 'allowed_regions' as a list of region IDs.")
        if not allowed:
            raise ValueError("Policy 'allowed_regions' must not be empty.")

        residency = raw.get("data_residency")
        jurisdiction = label = None
        if residency is not None:
            if not isinstance(residency, dict) or not isinstance(
                residency.get("jurisdiction"), str
            ):
                raise ValueError(
                    "'data_residency' must be an object with a 'jurisdiction' country code."
                )
            jurisdiction = residency["jurisdiction"].strip().upper()
            label = residency.get("label") if isinstance(residency.get("label"), str) else jurisdiction

        description = raw.get("description") if isinstance(raw.get("description"), str) else None
        return Policy(
            name=name.strip(),
            allowed_regions=[r.strip() for r in allowed],
            residency_jurisdiction=jurisdiction,
            residency_label=label,
            description=description,
        )


@dataclass
class GateResult:
    region: str
    known_region: bool
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_region(policy: Policy, region: str, catalog: CarbonCatalog) -> GateResult:
    """Judge one candidate region. Returns reasons for every violation found."""
    reasons: list[str] = []
    meta = catalog.get(region)

    if region not in policy.allowed_regions:
        reasons.append(
            f"Region '{region}' is not in the allowed region list "
            f"({', '.join(policy.allowed_regions)})."
        )

    if policy.residency_jurisdiction:
        if meta is None:
            reasons.append(
                f"Region '{region}' is not a recognized Google Cloud region, so its "
                f"jurisdiction cannot be verified against the residency requirement "
                f"({policy.residency_label})."
            )
        elif meta.country != policy.residency_jurisdiction:
            reasons.append(
                f"Region '{region}' ({meta.location}) is in {meta.country}, outside the "
                f"required jurisdiction {policy.residency_label}."
            )

    return GateResult(
        region=region,
        known_region=meta is not None,
        allowed=not reasons,
        reasons=reasons,
    )
