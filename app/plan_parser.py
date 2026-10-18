"""Terraform plan JSON parsing.

Only google_compute_instance resource changes are supported. Every other
resource type is reported as unsupported instead of being silently skipped.
Missing or malformed fields become explicit issues; nothing is guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SUPPORTED_RESOURCE_TYPES = {"google_compute_instance"}


@dataclass
class ResourceChange:
    address: str
    resource_type: str
    actions: list[str]
    before_zone: str | None
    after_zone: str | None
    before_region: str | None
    after_region: str | None
    machine_type: str | None
    supported: bool


@dataclass
class ParseResult:
    changes: list[ResourceChange] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def candidate_regions(self) -> list[str]:
        """Distinct after-regions from supported, non-delete changes."""
        seen: list[str] = []
        for change in self.changes:
            if not change.supported:
                continue
            if change.actions in (["delete"], ["no-op"], ["read"]):
                continue
            if change.after_region and change.after_region not in seen:
                seen.append(change.after_region)
        return seen

    @property
    def baseline_region(self) -> str | None:
        """Region the workload runs in today: first before-region seen."""
        for change in self.changes:
            if change.supported and change.before_region:
                return change.before_region
        return None


def zone_to_region(zone: str) -> str | None:
    """asia-northeast1-a -> asia-northeast1. Returns None if not zone-shaped."""
    parts = zone.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha() and parts[0]:
        return parts[0]
    return None


def _extract_zone(state: object) -> str | None:
    if isinstance(state, dict):
        zone = state.get("zone")
        if isinstance(zone, str) and zone:
            return zone
        region = state.get("region")
        if isinstance(region, str) and region:
            return region  # already a region; zone_to_region will leave it as-is below
    return None


def _extract_region(state: object) -> str | None:
    zone = _extract_zone(state)
    if zone is None:
        return None
    return zone_to_region(zone) or zone


def _extract_machine_type(state: object) -> str | None:
    if isinstance(state, dict):
        machine_type = state.get("machine_type")
        if isinstance(machine_type, str) and machine_type:
            return machine_type.rsplit("/", 1)[-1]
    return None


def parse_plan(plan: object) -> ParseResult:
    result = ParseResult()
    if not isinstance(plan, dict):
        result.issues.append("Plan input is not a JSON object.")
        return result

    resource_changes = plan.get("resource_changes")
    if not isinstance(resource_changes, list):
        result.issues.append(
            "Plan JSON has no 'resource_changes' list. "
            "Pass the output of `terraform show -json <plan>`."
        )
        return result

    if not resource_changes:
        result.issues.append("Plan JSON contains an empty 'resource_changes' list.")
        return result

    for index, entry in enumerate(resource_changes):
        if not isinstance(entry, dict):
            result.issues.append(f"resource_changes[{index}] is not an object; skipped.")
            continue

        address = entry.get("address") if isinstance(entry.get("address"), str) else f"resource_changes[{index}]"
        resource_type = entry.get("type") if isinstance(entry.get("type"), str) else None
        change = entry.get("change") if isinstance(entry.get("change"), dict) else {}

        if resource_type is None:
            result.issues.append(f"{address}: missing resource type; skipped.")
            continue

        actions = change.get("actions")
        if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
            result.issues.append(f"{address}: missing or invalid change.actions; skipped.")
            continue

        if actions in (["no-op"], ["read"]):
            continue

        if resource_type not in SUPPORTED_RESOURCE_TYPES:
            result.unsupported.append(
                {"address": address, "type": resource_type, "actions": actions}
            )
            continue

        before_region = _extract_region(change.get("before"))
        after_region = _extract_region(change.get("after"))

        if actions != ["delete"] and after_region is None:
            result.issues.append(
                f"{address}: no zone/region in the after state; region unknown, not guessed."
            )

        result.changes.append(
            ResourceChange(
                address=address,
                resource_type=resource_type,
                actions=actions,
                before_zone=_extract_zone(change.get("before")),
                after_zone=_extract_zone(change.get("after")),
                before_region=before_region,
                after_region=after_region,
                machine_type=_extract_machine_type(change.get("after"))
                or _extract_machine_type(change.get("before")),
                supported=True,
            )
        )

    if not result.changes and not result.unsupported and not result.issues:
        result.issues.append("No usable resource changes found in the plan.")

    return result
