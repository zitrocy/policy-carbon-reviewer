import pytest

from app.agents import AGENTS, DETERMINISTIC, MODEL, Trace
from app.carbon import CarbonCatalog
from app.reviewer import run_review

catalog = CarbonCatalog()

PLAN = {
    "resource_changes": [
        {
            "address": "google_compute_instance.app",
            "type": "google_compute_instance",
            "change": {
                "actions": ["update"],
                "before": {"zone": "asia-northeast1-a", "machine_type": "e2-standard-4"},
                "after": {"zone": "asia-northeast2-a", "machine_type": "e2-standard-4"},
            },
        }
    ]
}
POLICY = {"name": "JP only", "allowed_regions": ["asia-northeast1", "asia-northeast2"]}
BLOCKING_POLICY = {"name": "Osaka only", "allowed_regions": ["asia-northeast2"]}


class TestTrace:
    def test_step_records_timing_and_status(self):
        trace = Trace()
        with trace.step("plan-analyst") as step:
            step.summary = "done"
        assert len(trace.steps) == 1
        assert trace.steps[0].status == "ok"
        assert trace.steps[0].duration_ms >= 0

    def test_failing_step_is_recorded_then_reraised(self):
        trace = Trace()
        with pytest.raises(ValueError):
            with trace.step("policy-gate"):
                raise ValueError("boom")
        assert trace.steps[0].status == "failed"
        assert "boom" in trace.steps[0].summary

    def test_skip_records_without_timing(self):
        trace = Trace()
        trace.skip("policy-interpreter", "not needed")
        assert trace.steps[0].status == "skipped"
        assert trace.steps[0].duration_ms == 0.0

    def test_engine_comes_from_the_registry(self):
        trace = Trace()
        with trace.step("review-writer"):
            pass
        with trace.step("carbon-analyst"):
            pass
        assert trace.steps[0].engine == MODEL
        assert trace.steps[1].engine == DETERMINISTIC

    def test_registry_engines_are_valid(self):
        assert all(engine in (MODEL, DETERMINISTIC) for engine, _ in AGENTS.values())
        assert all(role for _, role in AGENTS.values())


class TestPipelineTrace:
    def test_every_agent_appears_in_the_trace(self):
        result = run_review(PLAN, POLICY, catalog)
        names = [s["name"] for s in result["agent_trace"]]
        assert names == list(AGENTS.keys())

    def test_decisions_are_made_by_deterministic_agents(self):
        """The gate and the ranking must never be model steps."""
        result = run_review(PLAN, POLICY, catalog)
        by_name = {s["name"]: s for s in result["agent_trace"]}
        for name in ("plan-analyst", "policy-gate", "carbon-analyst",
                     "impact-estimator", "verifier"):
            assert by_name[name]["engine"] == DETERMINISTIC

    def test_interpreter_is_skipped_for_structured_policy(self):
        result = run_review(PLAN, POLICY, catalog)
        step = next(s for s in result["agent_trace"] if s["name"] == "policy-interpreter")
        assert step["status"] == "skipped"
        assert result["policy"]["interpreted_from_text"] is False

    def test_gate_warns_when_a_region_is_blocked(self):
        result = run_review(PLAN, BLOCKING_POLICY, catalog)
        step = next(s for s in result["agent_trace"] if s["name"] == "policy-gate")
        assert step["status"] in ("ok", "warned")
        assert step["detail"]["allowed"] == ["asia-northeast2"]

    def test_blocked_review_reports_no_saving(self):
        plan = {
            "resource_changes": [
                {
                    "address": "google_compute_instance.app",
                    "type": "google_compute_instance",
                    "change": {
                        "actions": ["update"],
                        "before": {"zone": "asia-northeast1-a", "machine_type": "e2-standard-4"},
                        "after": {"zone": "europe-west9-a", "machine_type": "e2-standard-4"},
                    },
                }
            ]
        }
        result = run_review(plan, POLICY, catalog)
        assert result["verdict"] == "blocked"
        assert result["impact"]["total_delta_kg_co2e"] is None
        assert result["impact"]["total_target_kg_co2e"] is None
        # the current footprint is still known and reported
        assert result["impact"]["total_baseline_kg_co2e"] > 0

    def test_impact_block_is_always_present(self):
        result = run_review(PLAN, POLICY, catalog)
        assert result["impact"]["assumptions"]
        assert result["impact"]["estimated_count"] == 1

    def test_interpreter_failure_raises_rather_than_guessing(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(ValueError):
            run_review(PLAN, None, catalog, policy_text="Keep everything in Japan.")
