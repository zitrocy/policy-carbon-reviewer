"""Gemini review generation with code-side verification.

Design rule: code decides, Gemini explains. The model only receives the
policy-allowed candidate IDs and the numbers the code already computed. Its
JSON response is validated back against those facts; anything inconsistent is
rejected and the caller falls back to the code-generated summary, clearly
labeled, never presented as model output.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx

DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODEL = "gemini-2.5-flash"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
VERTEX_API_BASE = "https://aiplatform.googleapis.com/v1/publishers/google"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "recommended_region": {"type": "string"},
        "headline": {"type": "string"},
        "rationale": {"type": "array", "items": {"type": "string"}},
        "tradeoffs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["recommended_region", "headline", "rationale", "tradeoffs"],
}


@dataclass
class ReviewVerdict:
    status: str  # "gemini" | "offline" | "rejected" | "error"
    comment_markdown: str
    model: str | None = None
    note: str | None = None


def impact_lines(impact) -> list[str]:
    """Render the estimate for the prompt and the comment, or nothing."""
    if impact is None or impact.total_target_kg_co2e is None:
        return []
    lines = []
    if impact.total_delta_kg_co2e is not None:
        verb = "saves" if impact.total_delta_kg_co2e < 0 else "adds"
        lines.append(
            f"ESTIMATED ANNUAL IMPACT: moving to the recommended region {verb} about "
            f"{abs(impact.total_delta_kg_co2e):,.0f} kgCO2eq/year across "
            f"{impact.estimated_count} resource(s) "
            f"({impact.total_baseline_kg_co2e:,.0f} -> {impact.total_target_kg_co2e:,.0f})."
        )
    else:
        lines.append(
            f"ESTIMATED ANNUAL IMPACT: about {impact.total_target_kg_co2e:,.0f} "
            f"kgCO2eq/year across {impact.estimated_count} resource(s); no baseline "
            "to compare against."
        )
    if impact.total_new_kg_co2e:
        lines.append(
            f"NEWLY CREATED RESOURCES add about {impact.total_new_kg_co2e:,.0f} "
            f"kgCO2eq/year ({impact.created_count} resource(s)). This is an addition, "
            "not a saving, and is deliberately not netted against the migration figure."
        )
    lines.append(
        "This is an estimate derived from vCPU count, not a measurement. Say so if "
        "you cite it."
    )
    return lines


def build_prompt(
    policy_name: str,
    baseline_region: str | None,
    comparison_rows: list[dict],
    blocked: list[dict],
    source_label: str,
    impact=None,
) -> str:
    """Prompt contains only code-computed facts about allowed candidates."""
    lines = [
        "You are writing a short infrastructure review comment for a pull request.",
        "The deterministic policy engine has already decided everything below.",
        "Your job is only to explain it clearly. Do not recommend any region that is",
        "not listed under ALLOWED CANDIDATES. Do not invent numbers.",
        "",
        f"POLICY: {policy_name}",
        f"BASELINE REGION: {baseline_region or 'none (new workload)'}",
        f"CARBON DATA: {source_label}",
        "",
        "ALLOWED CANDIDATES (region, location, CFE%, grid gCO2eq/kWh, delta vs baseline):",
    ]
    for row in comparison_rows:
        delta = row["delta_pct_vs_baseline"]
        delta_text = "baseline" if delta == 0 else (f"{delta:+.1f}%" if delta is not None else "n/a")
        lines.append(
            f"- {row['region']} ({row['location']}): CFE {row['cfe_pct']:.0f}%, "
            f"grid {row['grid_carbon_intensity']:.0f} gCO2eq/kWh, delta {delta_text}"
        )
    extra = impact_lines(impact)
    if extra:
        lines.append("")
        lines.extend(extra)
    if blocked:
        lines.append("")
        lines.append("BLOCKED BY POLICY (never recommend these, you may mention they were excluded):")
        for item in blocked:
            lines.append(f"- {item['region']}: {' '.join(item['reasons'])}")
    lines.append("")
    lines.append(
        "Pick the allowed candidate with the lowest grid carbon intensity as "
        "recommended_region. Write a one-line headline, 2-4 rationale bullets that "
        "cite the given numbers, and 1-3 tradeoff bullets (e.g. latency, service "
        "availability, CFE% vs grid intensity are different metrics). If an "
        "estimated annual impact is given, cite it in the headline and label it "
        "as an estimate."
    )
    return "\n".join(lines)


def validate_review(data: object, allowed_region_ids: list[str]) -> tuple[bool, str | None]:
    """Check the model output against code-known facts."""
    if not isinstance(data, dict):
        return False, "Response is not a JSON object."
    for key in ("recommended_region", "headline", "rationale", "tradeoffs"):
        if key not in data:
            return False, f"Missing field '{key}'."
    if data["recommended_region"] not in allowed_region_ids:
        return False, (
            f"recommended_region '{data['recommended_region']}' is not an allowed candidate."
        )
    if allowed_region_ids and data["recommended_region"] != allowed_region_ids[0]:
        return False, (
            f"recommended_region '{data['recommended_region']}' is not the cleanest "
            f"allowed candidate ('{allowed_region_ids[0]}')."
        )
    if not isinstance(data["headline"], str) or not data["headline"].strip():
        return False, "headline is empty."
    for key in ("rationale", "tradeoffs"):
        value = data[key]
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, str) and v.strip() for v in value)
        ):
            return False, f"'{key}' must be a non-empty list of strings."
    return True, None


def render_comment(
    review: dict,
    comparison_rows: list[dict],
    baseline_region: str | None,
    source_label: str,
    impact=None,
) -> str:
    """Render a validated review dict as a PR-comment markdown block."""
    lines = [f"**{review['headline'].strip()}**", ""]
    lines.append(f"Recommended region: `{review['recommended_region']}`")
    lines.append("")
    lines.append("Why:")
    for bullet in review["rationale"]:
        lines.append(f"- {bullet.strip()}")
    if review["tradeoffs"]:
        lines.append("")
        lines.append("Trade-offs to check before merging:")
        for bullet in review["tradeoffs"]:
            lines.append(f"- {bullet.strip()}")
    lines.append("")
    lines.append("---")
    lines.append("| Region | CFE % | Grid gCO2eq/kWh | vs baseline |")
    lines.append("|---|---|---|---|")
    for row in comparison_rows:
        delta = row["delta_pct_vs_baseline"]
        delta_text = "baseline" if delta == 0 else (f"{delta:+.1f}%" if delta is not None else "n/a")
        lines.append(
            f"| {row['region']} ({row['location']}) | {row['cfe_pct']:.0f}% "
            f"| {row['grid_carbon_intensity']:.0f} | {delta_text} |"
        )
    if impact is not None and impact.total_target_kg_co2e is not None:
        lines.append("")
        if impact.total_delta_kg_co2e is not None:
            verb = "Estimated saving" if impact.total_delta_kg_co2e < 0 else "Estimated increase"
            lines.append(
                f"**{verb}: ~{abs(impact.total_delta_kg_co2e):,.0f} kgCO2eq/year** "
                f"across {impact.migrated_count} migrated resource(s) "
                f"({impact.total_baseline_kg_co2e:,.0f} -> "
                f"{impact.total_target_kg_co2e:,.0f} kgCO2eq/year)."
            )
        else:
            lines.append(
                f"**Estimated footprint: ~{impact.total_target_kg_co2e:,.0f} "
                f"kgCO2eq/year** across {impact.estimated_count} resource(s)."
            )
        if impact.total_new_kg_co2e:
            lines.append("")
            lines.append(
                f"This change also creates {impact.created_count} new resource(s), "
                f"adding about {impact.total_new_kg_co2e:,.0f} kgCO2eq/year. That is "
                "an addition, not a saving, so it is reported separately rather than "
                "netted off."
            )
        lines.append("")
        lines.append("<details><summary>How the estimate was made</summary>")
        lines.append("")
        for assumption in impact.assumptions:
            lines.append(f"- {assumption}")
        if impact.unestimated_count:
            lines.append(
                f"- {impact.unestimated_count} resource(s) had no recognizable machine "
                "shape and were left out rather than guessed."
            )
        lines.append("")
        lines.append("</details>")

    lines.append("")
    baseline_note = baseline_region or "none (new workload)"
    lines.append(f"Baseline: `{baseline_note}` · Carbon data: {source_label}")
    return "\n".join(lines)


def offline_review(
    comparison_rows: list[dict],
    baseline_region: str | None,
    source_label: str,
    impact=None,
) -> ReviewVerdict:
    """Deterministic template used when no GEMINI_API_KEY is configured.

    Clearly labeled as code-generated, never as model output.
    """
    best = comparison_rows[0]
    review = {
        "recommended_region": best["region"],
        "headline": (
            f"Policy passed. {best['region']} ({best['location']}) is the cleanest "
            "allowed region in this change."
        ),
        "rationale": [
            f"Grid carbon intensity is {best['grid_carbon_intensity']:.0f} gCO2eq/kWh "
            f"with {best['cfe_pct']:.0f}% carbon-free energy (annual averages).",
            "All candidates shown already passed the policy gate; blocked regions were excluded.",
        ],
        "tradeoffs": [
            "Grid metrics are annual averages, not a guarantee of workload emission cuts.",
            "Latency and service availability still need a separate check.",
        ],
    }
    if baseline_region:
        if best["region"] != baseline_region and best["delta_pct_vs_baseline"] is not None:
            review["rationale"].append(
                f"Compared with the current baseline {baseline_region}, "
                f"{best['region']} is {best['delta_pct_vs_baseline']:+.1f}% in grid intensity."
            )
        else:
            review["rationale"].append("The workload stays in its current region.")
    if impact is not None and impact.total_delta_kg_co2e is not None:
        verb = "cuts" if impact.total_delta_kg_co2e < 0 else "adds"
        review["rationale"].append(
            f"Estimated annual impact: {verb} about "
            f"{abs(impact.total_delta_kg_co2e):,.0f} kgCO2eq/year across "
            f"{impact.migrated_count} migrated resource(s) (estimate from vCPU count, "
            "not a measurement)."
        )
    comment = render_comment(review, comparison_rows, baseline_region, source_label, impact)
    return ReviewVerdict(
        status="offline",
        comment_markdown=comment,
        model=None,
        note="No GEMINI_API_KEY configured - this is a code-generated summary, not model output.",
    )


def _try_vertex_adc(payload: dict, model: str, fallback_model: str) -> tuple[dict | None, str]:
    """Fallback to Cloud Run metadata server OAuth token against Vertex AI when an API key is restricted."""
    try:
        meta_headers = {"Metadata-Flavor": "Google"}
        tok_res = httpx.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers=meta_headers,
            timeout=1.5,
        )
        tok_res.raise_for_status()
        token = tok_res.json().get("access_token")
        proj = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not proj:
            proj_res = httpx.get(
                "http://metadata.google.internal/computeMetadata/v1/project/project-id",
                headers=meta_headers,
                timeout=1.5,
            )
            proj_res.raise_for_status()
            proj = proj_res.text.strip()
        if not token or not proj:
            return None, fallback_model
        for m in ([model, FALLBACK_MODEL] if model == DEFAULT_MODEL else [model]):
            for loc in ("global", "us-central1"):
                url = (
                    f"https://aiplatform.googleapis.com/v1/projects/{proj}"
                    f"/locations/{loc}/publishers/google/models/{m}:generateContent"
                )
                r = httpx.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                    timeout=30.0,
                )
                if r.status_code == 200:
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(text), m
    except Exception:
        pass
    return None, fallback_model


def generate_review(
    policy_name: str,
    baseline_region: str | None,
    comparison_rows: list[dict],
    blocked: list[dict],
    source_label: str,
    impact=None,
) -> ReviewVerdict:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not comparison_rows:
        return ReviewVerdict(
            status="error",
            comment_markdown="",
            note="No allowed candidates to review.",
        )
    if not api_key:
        return offline_review(comparison_rows, baseline_region, source_label, impact)

    model = (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
    prompt = build_prompt(
        policy_name, baseline_region, comparison_rows, blocked, source_label, impact
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.2,
        },
    }
    candidates_to_try = [(API_BASE, model)]
    if model == DEFAULT_MODEL:
        candidates_to_try.append((API_BASE, FALLBACK_MODEL))
    if api_key.startswith("AQ."):
        candidates_to_try.append((VERTEX_API_BASE, model))
        if model == DEFAULT_MODEL:
            candidates_to_try.append((VERTEX_API_BASE, FALLBACK_MODEL))

    data = None
    last_exc: Exception | None = None
    used_model = model
    for base_url, candidate_model in candidates_to_try:
        url = f"{base_url}/models/{candidate_model}:generateContent"
        try:
            response = httpx.post(url, params={"key": api_key}, json=payload, timeout=30.0)
            response.raise_for_status()
            body = response.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            used_model = candidate_model
            break
        except Exception as exc:  # network, HTTP, shape, or JSON errors
            last_exc = exc
            continue

    if data is None:
        data, used_model = _try_vertex_adc(payload, model, used_model)

    if data is None:
        fallback = offline_review(comparison_rows, baseline_region, source_label, impact)
        return ReviewVerdict(
            status="error",
            comment_markdown=fallback.comment_markdown,
            model=used_model,
            note=f"Gemini call failed ({type(last_exc).__name__}); showing the code-generated summary instead.",
        )
    model = used_model

    allowed_ids = [row["region"] for row in comparison_rows]
    ok, reason = validate_review(data, allowed_ids)
    if not ok:
        fallback = offline_review(comparison_rows, baseline_region, source_label, impact)
        return ReviewVerdict(
            status="rejected",
            comment_markdown=fallback.comment_markdown,
            model=model,
            note=f"Gemini output failed verification ({reason}); showing the code-verified result only.",
        )

    return ReviewVerdict(
        status="gemini",
        comment_markdown=render_comment(
            data, comparison_rows, baseline_region, source_label, impact
        ),
        model=model,
        note=None,
    )
