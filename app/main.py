"""FastAPI entry point for the policy-compliant carbon reviewer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agents import AGENTS
from app.carbon import CarbonCatalog
from app.reviewer import run_review, source_label

BASE_DIR = Path(__file__).parent
SAMPLES_DIR = BASE_DIR.parent / "samples"
STATIC_DIR = BASE_DIR / "static"

catalog = CarbonCatalog()
app = FastAPI(title="Policy-Compliant Carbon Reviewer", version="1.2.0")


class ReviewRequest(BaseModel):
    plan: object
    # Either a structured policy, or prose for the interpreter agent to read.
    # When both are present, policy_text wins and the interpreter runs.
    policy: object = None
    policy_text: str | None = None


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/healthz")
def healthz():
    return {"status": "ok", "gemini_configured": bool(os.environ.get("GEMINI_API_KEY"))}


@app.get("/api/agents")
def agents():
    """The pipeline, so the architecture is inspectable without reading code."""
    return {
        "agents": [
            {"name": name, "engine": engine, "role": role}
            for name, (engine, role) in AGENTS.items()
        ]
    }


@app.get("/api/carbon-data")
def carbon_data():
    return {"metadata": catalog.metadata, "source": source_label(catalog)}


@app.get("/api/samples")
def samples():
    out = []
    for path in sorted(SAMPLES_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            {
                "id": path.stem,
                "title": raw.get("title", path.stem),
                "description": raw.get("description", ""),
                "plan": raw["plan"],
                "policy": raw["policy"],
                "policy_text": raw.get("policy_text"),
            }
        )
    return {"samples": out}


@app.post("/api/review")
def review(request: ReviewRequest):
    started = time.perf_counter()
    if request.policy is None and not (request.policy_text or "").strip():
        return JSONResponse(
            status_code=422,
            content={"detail": "Provide either 'policy' (JSON) or 'policy_text' (prose)."},
        )
    try:
        result = run_review(
            request.plan, request.policy, catalog, policy_text=request.policy_text
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
