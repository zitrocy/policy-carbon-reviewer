"""Agent pipeline definition and execution trace.

The review is produced by a pipeline of narrow agents, not one model call.
Five of them are deterministic code and two call Gemini; both kinds are
recorded the same way, because the point of the trace is to show *where* a
model is allowed to influence the result and where it is not.

The division is deliberate. A model is used only where the task is a language
task - reading a prose policy, writing a review comment. Every decision that
a reviewer would be angry to see a model get wrong (is this region allowed,
which region is cleanest, how much CO2eq) is computed in code, and the
verifier agent checks the model's output back against those computed facts.

The trace is returned with every review so the UI, and anyone reading the
API, can see the chain rather than take the final comment on trust.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

DETERMINISTIC = "deterministic"
MODEL = "gemini"

# name -> (engine, one-line role description)
AGENTS: dict[str, tuple[str, str]] = {
    "policy-interpreter": (
        MODEL,
        "Reads a prose policy and proposes a structured one; every region it "
        "returns is checked against the catalog before use.",
    ),
    "plan-analyst": (
        DETERMINISTIC,
        "Parses the Terraform plan, extracts regions and machine shapes, and "
        "reports anything it cannot read instead of guessing.",
    ),
    "policy-gate": (
        DETERMINISTIC,
        "Decides which target regions are permitted. Carbon data cannot "
        "override this decision.",
    ),
    "carbon-analyst": (
        DETERMINISTIC,
        "Ranks the permitted regions on official grid carbon intensity.",
    ),
    "impact-estimator": (
        DETERMINISTIC,
        "Turns machine shapes into an annual kgCO2eq estimate with its "
        "assumptions attached.",
    ),
    "review-writer": (
        MODEL,
        "Writes the PR comment from the computed facts only.",
    ),
    "verifier": (
        DETERMINISTIC,
        "Checks the written comment against the computed facts and rejects it "
        "if they disagree.",
    ),
}


@dataclass
class AgentStep:
    name: str
    engine: str
    role: str
    status: str  # "ok" | "skipped" | "warned" | "failed"
    summary: str
    duration_ms: float
    detail: dict = field(default_factory=dict)


@dataclass
class Trace:
    steps: list[AgentStep] = field(default_factory=list)

    @contextmanager
    def step(self, name: str):
        """Time one agent. The body sets ``.status``/``.summary``/``.detail``.

        A step that raises is recorded as failed and the error is re-raised,
        so a trace never silently loses a stage.
        """
        engine, role = AGENTS.get(name, (DETERMINISTIC, ""))
        record = AgentStep(
            name=name, engine=engine, role=role,
            status="ok", summary="", duration_ms=0.0,
        )
        started = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            record.status = "failed"
            record.summary = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            self.steps.append(record)

    def skip(self, name: str, summary: str) -> None:
        engine, role = AGENTS.get(name, (DETERMINISTIC, ""))
        self.steps.append(
            AgentStep(
                name=name, engine=engine, role=role,
                status="skipped", summary=summary, duration_ms=0.0,
            )
        )

    def as_list(self) -> list[dict]:
        return [asdict(step) for step in self.steps]

    @property
    def model_steps(self) -> int:
        return sum(1 for s in self.steps if s.engine == MODEL and s.status == "ok")
