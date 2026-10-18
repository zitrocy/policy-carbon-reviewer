"""Annual emission estimate for a reviewed change.

The region comparison answers "which allowed region has the cleanest grid".
This module answers the question a reviewer actually asks next: "how much
does this change matter?" - in kgCO2eq per year.

Honesty rules, in order of importance:

1. This is an **estimate from vCPU count**, not a measurement and not a bill.
   Google does not publish per-instance power draw, so the machine shape is
   turned into an assumed average power draw. Every assumption below is
   returned with the number so a reviewer can disagree with it.
2. Unknown machine types produce ``None``, never a guessed default. A missing
   estimate is reported as missing.
3. The grid intensity used is the same official annual average used
   everywhere else in the app, so the estimate inherits that metric's limits:
   it is an annual average for a grid, not the marginal emissions of one
   workload at one moment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Average power draw assumed per vCPU, in watts, already accounting for an
# assumed average utilization. Public per-instance figures do not exist, so
# this is a transparent planning constant, not a vendor number.
WATTS_PER_VCPU = 7.0

# Assumed share of the year the instance is running. 1.0 = always on, which
# is the right default for a VM declared in Terraform without a schedule.
ASSUMED_DUTY_CYCLE = 1.0

# Power usage effectiveness: overhead for cooling and power delivery on top
# of server draw. Google reports a fleet-wide average near 1.1; used here as
# a stated assumption, not as a claim about any specific data center.
ASSUMED_PUE = 1.1

HOURS_PER_YEAR = 24 * 365

# Shared-core and legacy machine types whose names carry no vCPU count.
SHARED_CORE_VCPU = {
    "f1-micro": 0.2,
    "g1-small": 0.5,
    "e2-micro": 0.25,
    "e2-small": 0.5,
    "e2-medium": 1.0,
}

# families like n2-standard-8, c3-highcpu-4, e2-highmem-16, n2d-standard-2
_SHAPE_RE = re.compile(r"^[a-z0-9]+-(?:standard|highmem|highcpu|megamem|ultramem)-(\d+)$")

# families like c3-standard-8-lssd where a suffix follows the vCPU count
_SHAPE_SUFFIX_RE = re.compile(
    r"^[a-z0-9]+-(?:standard|highmem|highcpu|megamem|ultramem)-(\d+)-[a-z]+$"
)


def vcpu_count(machine_type: str | None) -> float | None:
    """vCPU count for a machine type, or None when it cannot be known.

    Custom shapes (``custom-4-8192``, ``n2-custom-8-16384``) are supported
    because the vCPU count is part of the name. Anything unrecognized returns
    None so the caller can report "no estimate" rather than invent one.
    """
    if not machine_type or not isinstance(machine_type, str):
        return None
    name = machine_type.strip().lower()
    if not name:
        return None

    if name in SHARED_CORE_VCPU:
        return SHARED_CORE_VCPU[name]

    match = _SHAPE_RE.match(name) or _SHAPE_SUFFIX_RE.match(name)
    if match:
        return float(int(match.group(1)))

    # custom-<vcpu>-<memory_mb> and <family>-custom-<vcpu>-<memory_mb>
    parts = name.split("-")
    if "custom" in parts:
        idx = parts.index("custom")
        if idx + 1 < len(parts) and parts[idx + 1].isdigit():
            return float(int(parts[idx + 1]))

    return None


def annual_kwh(vcpus: float) -> float:
    """Assumed annual energy use for a given vCPU count, in kWh."""
    watts = vcpus * WATTS_PER_VCPU * ASSUMED_DUTY_CYCLE
    return watts / 1000.0 * HOURS_PER_YEAR * ASSUMED_PUE


def annual_kg_co2e(vcpus: float, grid_carbon_intensity: float) -> float:
    """Assumed annual emissions, in kgCO2eq, at a given grid intensity."""
    return annual_kwh(vcpus) * grid_carbon_intensity / 1000.0


@dataclass
class ResourceImpact:
    address: str
    machine_type: str | None
    vcpus: float | None
    baseline_region: str | None
    target_region: str | None
    baseline_kg_co2e: float | None
    target_kg_co2e: float | None
    delta_kg_co2e: float | None  # negative = reduction
    note: str | None = None


@dataclass
class ImpactEstimate:
    """Portfolio-level estimate across every supported change in the plan.

    Migrations and new resources are kept apart on purpose. A plan that moves
    one VM to a cleaner region while creating another is not "a saving of the
    difference": adding a workload adds emissions, and netting the two off
    would hide that. So the delta covers only resources that exist on both
    sides of the change, and newly created resources are totalled separately.
    """

    resources: list[ResourceImpact] = field(default_factory=list)
    # Migrations only: resources with both a before- and an after-region.
    total_baseline_kg_co2e: float | None = None
    total_target_kg_co2e: float | None = None
    total_delta_kg_co2e: float | None = None
    # Newly created resources, which add emissions rather than save any.
    total_new_kg_co2e: float | None = None
    migrated_count: int = 0
    created_count: int = 0
    estimated_count: int = 0
    unestimated_count: int = 0
    assumptions: list[str] = field(default_factory=list)


def assumption_lines() -> list[str]:
    return [
        f"{WATTS_PER_VCPU:g} W average draw per vCPU (planning constant, not a vendor figure).",
        f"{ASSUMED_DUTY_CYCLE * 100:g}% duty cycle - the instance is assumed to run all year.",
        f"PUE {ASSUMED_PUE:g} for cooling and power-delivery overhead.",
        "Grid carbon intensity is the official annual average for the region, "
        "not the marginal intensity at the hour the workload runs.",
        "Estimate covers compute only: no storage, network, or embodied emissions.",
    ]


def estimate_plan_impact(
    changes: list,
    catalog,
    recommended_region: str | None = None,
    allow_plan_target: bool = True,
) -> ImpactEstimate:
    """Estimate the annual emission change for every supported resource change.

    ``changes`` are ``ResourceChange`` objects from the plan parser.

    The target region is, in order: the policy-approved ``recommended_region``
    when there is one, otherwise the plan's own target - but only if
    ``allow_plan_target`` is set. The caller clears that flag when the policy
    gate blocked every candidate, because costing a move the policy forbids
    would report a "saving" for a change that must not happen. In that case
    only the current footprint is reported, with no target and no delta.
    """
    estimate = ImpactEstimate(assumptions=assumption_lines())

    baseline_total = 0.0   # migrations only
    target_total = 0.0     # migrations only
    new_total = 0.0        # newly created resources
    baseline_only_total = 0.0  # running today, with no costable destination
    saw_pair = False

    for change in changes:
        if not getattr(change, "supported", False):
            continue
        if change.actions in (["delete"], ["no-op"], ["read"]):
            continue

        vcpus = vcpu_count(change.machine_type)
        if recommended_region:
            target_region = recommended_region
        elif allow_plan_target:
            target_region = change.after_region
        else:
            target_region = None
        baseline_region = change.before_region

        base_meta = catalog.get(baseline_region) if baseline_region else None
        target_meta = catalog.get(target_region) if target_region else None

        note = None
        if vcpus is None:
            note = (
                f"No vCPU count for machine type "
                f"{change.machine_type or 'unspecified'}; no estimate made."
            )
        elif target_region is None:
            note = (
                "No policy-approved target region, so no post-change estimate was "
                "made. Only the current footprint is shown."
            )
        elif target_meta is None:
            note = f"No official carbon data for '{target_region}'; no estimate made."

        # Round per resource first, then total the rounded values. The totals
        # are shown next to the per-resource rows, so they have to add up on
        # screen; a total that is 0.1 off from its own rows reads as a bug.
        base_kg = (
            round(annual_kg_co2e(vcpus, base_meta.grid_carbon_intensity), 1)
            if vcpus is not None and base_meta
            else None
        )
        target_kg = (
            round(annual_kg_co2e(vcpus, target_meta.grid_carbon_intensity), 1)
            if vcpus is not None and target_meta
            else None
        )
        delta = (
            round(target_kg - base_kg, 1)
            if base_kg is not None and target_kg is not None
            else None
        )

        if target_kg is None:
            estimate.unestimated_count += 1
            if base_kg is not None:
                # No destination to cost, but what runs today is still known.
                baseline_only_total += base_kg
        else:
            estimate.estimated_count += 1
            if base_kg is not None:
                # A migration: both sides known, so a delta is meaningful.
                baseline_total += base_kg
                target_total += target_kg
                estimate.migrated_count += 1
                saw_pair = True
            else:
                # Newly created: added emissions, never netted against a saving.
                new_total += target_kg
                estimate.created_count += 1

        estimate.resources.append(
            ResourceImpact(
                address=change.address,
                machine_type=change.machine_type,
                vcpus=vcpus,
                baseline_region=baseline_region,
                target_region=target_region,
                baseline_kg_co2e=base_kg,
                target_kg_co2e=target_kg,
                delta_kg_co2e=delta,
                note=note,
            )
        )

    if saw_pair:
        estimate.total_baseline_kg_co2e = round(baseline_total, 1)
        estimate.total_target_kg_co2e = round(target_total, 1)
        estimate.total_delta_kg_co2e = round(target_total - baseline_total, 1)
    elif baseline_only_total:
        # Nothing could be costed on the target side (no approved destination,
        # or no carbon data), but the current footprint is still known.
        estimate.total_baseline_kg_co2e = round(baseline_only_total, 1)
    if new_total:
        estimate.total_new_kg_co2e = round(new_total, 1)

    return estimate
