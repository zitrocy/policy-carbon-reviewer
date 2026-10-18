import json
from pathlib import Path

import pytest

from app.reviewer import run_review

SAMPLES = Path(__file__).parent.parent / "samples"


def load_sample(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


class TestSampleScenarios:
    def test_scene1_greener_but_blocked(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        sample = load_sample("1-blocked-greener-region.json")
        result = run_review(sample["plan"], sample["policy"])
        assert result["verdict"] == "blocked"
        assert result["gate"][0]["region"] == "europe-west9"
        assert not result["gate"][0]["allowed"]
        # Greener region produced no recommendation at all.
        assert result["comparison"] == []
        assert result["review"] is None
        assert result["baseline_region"] == "asia-northeast1"

    def test_scene2_compare_allowed(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        sample = load_sample("2-compare-allowed.json")
        result = run_review(sample["plan"], sample["policy"])
        assert result["verdict"] == "ok"
        rows = result["comparison"]
        assert rows[0]["region"] == "asia-northeast2"
        assert rows[0]["delta_pct_vs_baseline"] == -33.4
        assert result["review"]["status"] == "offline"
        assert "not model output" in result["review"]["note"]

    def test_scene3_unknown_flagged_not_guessed(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        sample = load_sample("3-unknown-input.json")
        result = run_review(sample["plan"], sample["policy"])
        # atlantis-central1 is not allowed (not in list + jurisdiction unverifiable)
        assert result["verdict"] == "blocked"
        gate = {g["region"]: g for g in result["gate"]}
        assert not gate["atlantis-central1"]["known_region"]
        # Unsupported type reported, delete excluded from candidates
        assert result["resources"]["unsupported"][0]["type"] == "google_sql_database_instance"
        assert "asia-northeast1" not in result["candidate_regions"]

    def test_result_shape(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        sample = load_sample("2-compare-allowed.json")
        result = run_review(sample["plan"], sample["policy"])
        for key in ("verdict", "policy", "baseline_region", "candidate_regions",
                    "gate", "unknown_allowed_regions", "comparison", "resources",
                    "review", "carbon_data"):
            assert key in result
        assert result["carbon_data"]["data_year"] == 2025

    def test_bad_policy_raises_value_error(self):
        sample = load_sample("2-compare-allowed.json")
        with pytest.raises(ValueError):
            run_review(sample["plan"], {"allowed_regions": []})

    def test_malformed_plan_yields_no_candidates(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        sample = load_sample("2-compare-allowed.json")
        result = run_review({"resource_changes": "broken"}, sample["policy"])
        assert result["verdict"] == "no_candidates"
        assert result["resources"]["issues"]

    def test_multi_candidate_all_allowed(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        plan = {"resource_changes": [
            {"address": "a", "type": "google_compute_instance",
             "change": {"actions": ["create"], "before": None,
                        "after": {"zone": "asia-northeast1-a"}}},
            {"address": "b", "type": "google_compute_instance",
             "change": {"actions": ["create"], "before": None,
                        "after": {"zone": "asia-northeast2-a"}}},
        ]}
        policy = {"name": "p", "allowed_regions": ["asia-northeast1", "asia-northeast2"]}
        result = run_review(plan, policy)
        assert result["verdict"] == "ok"
        assert len(result["comparison"]) == 2
        assert result["baseline_region"] is None
