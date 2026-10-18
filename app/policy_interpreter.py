"""Natural-language policy -> structured policy JSON, verified by code.

This is the one step in the pipeline where a model earns its place. Policies
arrive as prose ("production data has to stay in Japan; dev can go anywhere
in APAC"), and turning prose into an allowed-region list is a language task.
Everything downstream stays deterministic.

The same discipline as the review writer applies, in reverse: the model
proposes, code disposes.

- The prompt carries the full list of real region IDs with their countries,
  so the model selects from a closed set instead of recalling region names.
- Every region in the response is checked against the catalog. One invented
  region fails the whole interpretation - it is not silently dropped, because
  a policy that quietly lost a region is more dangerous than no policy.
- The jurisdiction must be a 2-letter code that at least one allowed region
  actually sits in.
- The result is then parsed by ``Policy.from_dict`` like any hand-written
  policy, so it cannot bypass the normal validation.
- Any failure returns a reason and no policy. There is no fallback guess:
  silently inventing a compliance policy is the worst thing this app could do.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import httpx

from app.carbon import CarbonCatalog
from app.policy import Policy

DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODEL = "gemini-2.5-flash"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
VERTEX_API_BASE = "https://aiplatform.googleapis.com/v1/publishers/google"

POLICY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "allowed_regions": {"type": "array", "items": {"type": "string"}},
        "data_residency": {
            "type": "object",
            "properties": {
                "jurisdiction": {"type": "string"},
                "label": {"type": "string"},
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "allowed_regions", "assumptions"],
}


@dataclass
class InterpretedPolicy:
    ok: bool
    policy_dict: dict | None = None
    policy: Policy | None = None
    assumptions: list[str] = field(default_factory=list)
    model: str | None = None
    reason: str | None = None


def build_prompt(text: str, catalog: CarbonCatalog) -> str:
    """Prompt the model with the closed set of real regions."""
    region_lines = [
        f"- {meta.region} ({meta.location}, {meta.country})"
        for meta in sorted(catalog.all(), key=lambda m: m.region)
    ]
    return "\n".join(
        [
            "Convert an infrastructure policy written in prose into a strict JSON policy.",
            "",
            "Rules:",
            "- allowed_regions MUST be chosen only from the REGION CATALOG below.",
            "  Never invent a region ID. Never abbreviate one.",
            "- If the policy names a country or area rather than regions, expand it to",
            "  every catalog region in that country or area.",
            "- Set data_residency only when the text requires data to stay in one",
            "  country. jurisdiction is that country's 2-letter ISO code.",
            "- If the text is ambiguous, choose the NARROWER reading (fewer allowed",
            "  regions) and record the ambiguity in assumptions.",
            "- assumptions must list every interpretation you made that the text did",
            "  not state outright. An empty list means the text was fully explicit.",
            "",
            "REGION CATALOG:",
            *region_lines,
            "",
            "POLICY TEXT:",
            text.strip(),
        ]
    )


def verify(data: object, catalog: CarbonCatalog) -> tuple[bool, str | None]:
    """Check a proposed policy against the region catalog."""
    if not isinstance(data, dict):
        return False, "Response is not a JSON object."

    regions = data.get("allowed_regions")
    if not isinstance(regions, list) or not regions:
        return False, "'allowed_regions' is missing or empty."
    if not all(isinstance(r, str) for r in regions):
        return False, "'allowed_regions' must contain only strings."

    unknown = [r for r in regions if not catalog.known(r)]
    if unknown:
        return False, (
            "Interpretation rejected: not real Google Cloud regions - "
            + ", ".join(sorted(unknown))
        )

    residency = data.get("data_residency")
    if residency is not None:
        if not isinstance(residency, dict):
            return False, "'data_residency' must be an object."
        code = residency.get("jurisdiction")
        if not isinstance(code, str) or len(code.strip()) != 2:
            return False, "'data_residency.jurisdiction' must be a 2-letter country code."
        code = code.strip().upper()
        countries = {catalog.get(r).country for r in regions}
        if code not in countries:
            return False, (
                f"Residency jurisdiction '{code}' does not match any allowed region "
                f"(allowed regions are in: {', '.join(sorted(countries))})."
            )

    if not isinstance(data.get("name"), str) or not data["name"].strip():
        return False, "'name' is missing or empty."

    assumptions = data.get("assumptions")
    if not isinstance(assumptions, list) or not all(isinstance(a, str) for a in assumptions):
        return False, "'assumptions' must be a list of strings."

    return True, None


def to_policy_dict(data: dict) -> dict:
    """Strip the model-only fields, leaving a policy the normal parser accepts."""
    out = {
        "name": data["name"].strip(),
        "allowed_regions": [r.strip() for r in data["allowed_regions"]],
    }
    if isinstance(data.get("description"), str) and data["description"].strip():
        out["description"] = data["description"].strip()
    residency = data.get("data_residency")
    if isinstance(residency, dict) and isinstance(residency.get("jurisdiction"), str):
        entry = {"jurisdiction": residency["jurisdiction"].strip().upper()}
        if isinstance(residency.get("label"), str) and residency["label"].strip():
            entry["label"] = residency["label"].strip()
        out["data_residency"] = entry
    return out


def interpret_policy(text: str, catalog: CarbonCatalog | None = None) -> InterpretedPolicy:
    """Turn prose into a verified Policy, or explain why it could not be done."""
    catalog = catalog or CarbonCatalog()

    if not isinstance(text, str) or not text.strip():
        return InterpretedPolicy(ok=False, reason="Policy text is empty.")

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return InterpretedPolicy(
            ok=False,
            reason=(
                "Natural-language policy interpretation needs GEMINI_API_KEY. "
                "Paste a structured policy JSON instead - the rest of the review "
                "runs without a model."
            ),
        )

    model = (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": build_prompt(text, catalog)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": POLICY_SCHEMA,
            "temperature": 0.0,
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
        try:
            response = httpx.post(
                f"{base_url}/models/{candidate_model}:generateContent",
                params={"key": api_key},
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            body = response.json()
            data = json.loads(body["candidates"][0]["content"]["parts"][0]["text"])
            used_model = candidate_model
            break
        except Exception as exc:
            last_exc = exc
            continue

    if data is None:
        from app.gemini import _try_vertex_adc

        data, used_model = _try_vertex_adc(payload, model, used_model)

    if data is None:
        return InterpretedPolicy(
            ok=False,
            model=used_model,
            reason=f"Policy interpretation call failed ({type(last_exc).__name__}).",
        )
    model = used_model

    ok, reason = verify(data, catalog)
    if not ok:
        return InterpretedPolicy(ok=False, model=model, reason=reason)

    policy_dict = to_policy_dict(data)
    try:
        policy = Policy.from_dict(policy_dict)
    except ValueError as exc:
        return InterpretedPolicy(
            ok=False, model=model, reason=f"Interpreted policy is invalid: {exc}"
        )

    return InterpretedPolicy(
        ok=True,
        policy_dict=policy_dict,
        policy=policy,
        assumptions=[a.strip() for a in data.get("assumptions", []) if a.strip()],
        model=model,
    )
