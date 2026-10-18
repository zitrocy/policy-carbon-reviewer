"""Orchestration.

Pipeline: (policy interpreter) -> plan analyst -> policy gate ->
carbon analyst -> impact estimator -> review writer -> verifier.

Each stage is recorded in an execution trace (app/agents.py) so the response
shows which stages were deterministic and which involved a model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict

from app.agents import Trace
from app.carbon import CarbonCatalog
from app.gemini import generate_review
from app.impact import estimate_plan_impact
from app.plan_parser import parse_plan
from app.policy import Policy, evaluate_region
from app.policy_interpreter import interpret_policy


def source_label(catalog: CarbonCatalog) -> str:
    meta = catalog.metadata
    return (
        f"Google Cloud official {meta['data_year']} annual data "
        f"({meta['source_url']}, retrieved {meta['retrieved_at']})"
    )


def resolve_policy(
    policy_raw: object,
    policy_text: str | None,
    catalog: CarbonCatalog,
    trace: Trace,
) -> tuple[Policy, dict | None, list[str]]:
    """Get a Policy either from JSON as given, or by interpreting prose.

    Returns the policy, the interpreted policy dict (when prose was used), and
    the interpreter's stated assumptions. Raises ValueError when prose cannot
    be interpreted safely - there is no guessed fallback policy.
    """
    if policy_text and str(policy_text).strip():
        with trace.step("policy-interpreter") as step:
            result = interpret_policy(str(policy_text), catalog)
            if not result.ok:
                step.status = "failed"
                step.summary = result.reason or "Could not interpret the policy text."
                raise ValueError(step.summary)
            step.summary = (
                f"Interpreted prose into {len(result.policy.allowed_regions)} allowed "
                f"region(s)"
                + (
                    f"; residency {result.policy.residency_jurisdiction}"
                    if result.policy.residency_jurisdiction
                    else ""
                )
                + "."
            )
            step.detail = {
                "model": result.model,
                "allowed_regions": result.policy.allowed_regions,
                "assumptions": result.assumptions,
            }
            return result.policy, result.policy_dict, result.assumptions

    trace.skip("policy-interpreter", "Structured policy supplied; no interpretation needed.")
    return Policy.from_dict(policy_raw), None, []


def run_review(
    plan: object,
    policy_raw: object,
    catalog: CarbonCatalog | None = None,
    policy_text: str | None = None,
) -> dict:
    catalog = catalog or CarbonCatalog()
    trace = Trace()

    policy, interpreted_dict, assumptions = resolve_policy(
        policy_raw, policy_text, catalog, trace
    )

    with trace.step("plan-analyst") as step:
        parsed = parse_plan(plan)
        candidates = parsed.candidate_regions
        baseline = parsed.baseline_region
        step.summary = (
            f"{len(parsed.changes)} supported change(s), "
            f"{len(candidates)} target region(s), "
            f"{len(parsed.unsupported)} unsupported, {len(parsed.issues)} issue(s)."
        )
        if parsed.issues or parsed.unsupported:
            step.status = "warned"
        step.detail = {
            "candidate_regions": candidates,
            "baseline_region": baseline,
            "unsupported_types": sorted({u["type"] for u in parsed.unsupported}),
        }

    with trace.step("policy-gate") as step:
        gate_results = [evaluate_region(policy, region, catalog) for region in candidates]
        blocked = [g for g in gate_results if not g.allowed]
        allowed = [g.region for g in gate_results if g.allowed]
        unknown_allowed = [r for r in allowed if not catalog.known(r)]
        step.summary = f"{len(allowed)} allowed, {len(blocked)} blocked."
        if blocked:
            step.status = "warned"
        step.detail = {
            "allowed": allowed,
            "blocked": [{"region": g.region, "reasons": g.reasons} for g in blocked],
        }

    with trace.step("carbon-analyst") as step:
        # Show the baseline region in the table when it is itself allowed, so
        # the delta is visible in context. Blocked regions never appear here.
        compare_regions = list(allowed)
        if allowed and baseline and baseline not in compare_regions:
            baseline_gate = evaluate_region(policy, baseline, catalog)
            if baseline_gate.allowed and catalog.known(baseline):
                compare_regions.append(baseline)
        comparison_rows = [
            dict(asdict(row), is_baseline=(row.region == baseline))
            for row in catalog.compare(compare_regions, baseline)
        ]
        best = comparison_rows[0]["region"] if comparison_rows else None
        step.summary = (
            f"Ranked {len(comparison_rows)} allowed region(s); cleanest is {best}."
            if comparison_rows
            else "No allowed region had carbon data to rank."
        )
        if not comparison_rows:
            step.status = "warned"
        step.detail = {"ranking": [r["region"] for r in comparison_rows]}

    with trace.step("impact-estimator") as step:
        # When the gate blocked everything there is no approved destination, so
        # the plan's own target must not be costed: reporting a "saving" for a
        # forbidden move is exactly the mistake this tool exists to prevent.
        impact = estimate_plan_impact(
            parsed.changes,
            catalog,
            recommended_region=best,
            allow_plan_target=bool(allowed),
        )
        if impact.total_delta_kg_co2e is not None:
            direction = "reduction" if impact.total_delta_kg_co2e < 0 else "increase"
            step.summary = (
                f"Estimated {abs(impact.total_delta_kg_co2e):,.0f} kgCO2eq/year "
                f"{direction} across {impact.migrated_count} migrated resource(s)."
                + (
                    f" {impact.created_count} new resource(s) add "
                    f"{impact.total_new_kg_co2e:,.0f} kgCO2eq/year, reported separately."
                    if impact.total_new_kg_co2e
                    else ""
                )
            )
        elif impact.total_baseline_kg_co2e is not None:
            step.summary = (
                f"Current footprint about {impact.total_baseline_kg_co2e:,.0f} "
                "kgCO2eq/year; no policy-approved alternative to compare it with."
            )
        elif impact.total_new_kg_co2e is not None:
            step.summary = (
                f"All {impact.created_count} resource(s) are new, adding about "
                f"{impact.total_new_kg_co2e:,.0f} kgCO2eq/year; nothing to compare against."
            )
        else:
            step.status = "warned"
            step.summary = "No estimate possible from the machine shapes in this plan."
        step.detail = {
            "estimated": impact.estimated_count,
            "unestimated": impact.unestimated_count,
        }

    review = None
    if comparison_rows:
        with trace.step("review-writer") as step:
            review = generate_review(
                policy_name=policy.name,
                baseline_region=baseline,
                comparison_rows=comparison_rows,
                blocked=[{"region": g.region, "reasons": g.reasons} for g in blocked],
                source_label=source_label(catalog),
                impact=impact,
            )
            step.status = "ok" if review.status == "gemini" else "warned"
            step.summary = {
                "gemini": f"Comment written by {review.model}.",
                "offline": "No API key; deterministic summary used instead.",
                "rejected": "Model output failed verification; deterministic summary used.",
                "error": "Model call failed; deterministic summary used.",
            }.get(review.status, review.status)
            step.detail = {"status": review.status, "model": review.model}

        with trace.step("verifier") as step:
            if review.status == "gemini":
                step.summary = (
                    "Recommended region and all cited numbers matched the computed facts."
                )
            elif review.status == "rejected":
                step.status = "warned"
                step.summary = "Rejected the model's comment; served code-verified output."
            else:
                step.status = "skipped"
                step.summary = "Nothing from a model to verify."
            step.detail = {"allowed_candidates": [r["region"] for r in comparison_rows]}
    else:
        trace.skip("review-writer", "No allowed candidate region to write about.")
        trace.skip("verifier", "No model output to verify.")

    def gate_carbon(region: str) -> dict | None:
        """Carbon facts for a gate row, so a blocked region can show the number
        it was blocked despite - the core demo hook."""
        meta = catalog.get(region)
        if meta is None:
            return None
        delta = None
        base = catalog.get(baseline) if baseline else None
        if base and base.grid_carbon_intensity > 0:
            delta = round(
                (meta.grid_carbon_intensity - base.grid_carbon_intensity)
                / base.grid_carbon_intensity * 100, 1
            )
        return {
            "cfe_pct": meta.cfe_pct,
            "grid_carbon_intensity": meta.grid_carbon_intensity,
            "delta_pct_vs_baseline": delta,
        }

    if not candidates:
        verdict = "no_candidates"
    elif not allowed:
        verdict = "blocked"
    else:
        verdict = "ok"

    return {
        # Stamped when the review ran, so a stored audit record can say when a
        # decision was made rather than when it was later looked at.
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "policy": {
            "name": policy.name,
            "allowed_regions": policy.allowed_regions,
            "residency_jurisdiction": policy.residency_jurisdiction,
            "residency_label": policy.residency_label,
            "description": policy.description,
            "interpreted_from_text": interpreted_dict is not None,
            "interpreted_policy": interpreted_dict,
            "interpretation_assumptions": assumptions,
        },
        "baseline_region": baseline,
        "candidate_regions": candidates,
        "gate": [
            {
                "region": g.region,
                "known_region": g.known_region,
                "allowed": g.allowed,
                "reasons": g.reasons,
                "carbon": gate_carbon(g.region),
            }
            for g in gate_results
        ],
        "unknown_allowed_regions": unknown_allowed,
        "comparison": comparison_rows,
        "impact": {
            "total_baseline_kg_co2e": impact.total_baseline_kg_co2e,
            "total_target_kg_co2e": impact.total_target_kg_co2e,
            "total_delta_kg_co2e": impact.total_delta_kg_co2e,
            "total_new_kg_co2e": impact.total_new_kg_co2e,
            "migrated_count": impact.migrated_count,
            "created_count": impact.created_count,
            "estimated_count": impact.estimated_count,
            "unestimated_count": impact.unestimated_count,
            "assumptions": impact.assumptions,
            "resources": [asdict(r) for r in impact.resources],
        },
        "resources": {
            "supported_changes": [
                {
                    "address": c.address,
                    "actions": c.actions,
                    "before_region": c.before_region,
                    "after_region": c.after_region,
                    "machine_type": c.machine_type,
                }
                for c in parsed.changes
            ],
            "unsupported": parsed.unsupported,
            "issues": parsed.issues,
        },
        "review": (
            {
                "status": review.status,
                "model": review.model,
                "note": review.note,
                "comment_markdown": review.comment_markdown,
            }
            if review
            else None
        ),
        "agent_trace": trace.as_list(),
        "carbon_data": catalog.metadata,
    }
