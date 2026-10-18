"""Verification tests for the policy interpreter.

These cover the code side only - the part that decides whether a model's
proposed policy may be used at all. No network call is made.
"""

import pytest

from app.carbon import CarbonCatalog
from app.policy_interpreter import (
    build_prompt,
    interpret_policy,
    to_policy_dict,
    verify,
)

catalog = CarbonCatalog()


def good():
    return {
        "name": "Japan only",
        "description": "Production data stays in Japan.",
        "allowed_regions": ["asia-northeast1", "asia-northeast2"],
        "data_residency": {"jurisdiction": "JP", "label": "Japan"},
        "assumptions": ["Read 'Japan' as both Japanese regions."],
    }


class TestVerify:
    def test_accepts_a_well_formed_policy(self):
        ok, reason = verify(good(), catalog)
        assert ok and reason is None

    def test_rejects_an_invented_region(self):
        data = good()
        data["allowed_regions"] = ["asia-northeast1", "asia-northeast9"]
        ok, reason = verify(data, catalog)
        assert not ok
        assert "asia-northeast9" in reason

    def test_an_invented_region_fails_the_whole_policy(self):
        """A bad region must not be silently dropped: a policy that quietly
        lost a region is more dangerous than no policy at all."""
        data = good()
        data["allowed_regions"] = ["not-a-region"]
        ok, _ = verify(data, catalog)
        assert not ok

    def test_rejects_empty_region_list(self):
        data = good()
        data["allowed_regions"] = []
        assert not verify(data, catalog)[0]

    def test_rejects_residency_that_matches_no_allowed_region(self):
        data = good()
        data["data_residency"] = {"jurisdiction": "FR", "label": "France"}
        ok, reason = verify(data, catalog)
        assert not ok
        assert "FR" in reason

    def test_rejects_malformed_jurisdiction(self):
        data = good()
        data["data_residency"] = {"jurisdiction": "JAPAN"}
        assert not verify(data, catalog)[0]

    def test_residency_is_optional(self):
        data = good()
        del data["data_residency"]
        assert verify(data, catalog)[0]

    def test_rejects_missing_name(self):
        data = good()
        data["name"] = "   "
        assert not verify(data, catalog)[0]

    def test_rejects_non_object(self):
        assert not verify(["asia-northeast1"], catalog)[0]
        assert not verify(None, catalog)[0]

    def test_requires_assumptions_list(self):
        data = good()
        data["assumptions"] = "I assumed things"
        assert not verify(data, catalog)[0]

    def test_empty_assumptions_list_is_allowed(self):
        data = good()
        data["assumptions"] = []
        assert verify(data, catalog)[0]


class TestToPolicyDict:
    def test_strips_model_only_fields(self):
        out = to_policy_dict(good())
        assert "assumptions" not in out
        assert out["allowed_regions"] == ["asia-northeast1", "asia-northeast2"]

    def test_normalizes_jurisdiction_case(self):
        data = good()
        data["data_residency"] = {"jurisdiction": "jp"}
        assert to_policy_dict(data)["data_residency"]["jurisdiction"] == "JP"

    def test_output_is_accepted_by_the_normal_policy_parser(self):
        from app.policy import Policy

        policy = Policy.from_dict(to_policy_dict(good()))
        assert policy.residency_jurisdiction == "JP"
        assert len(policy.allowed_regions) == 2


class TestPrompt:
    def test_prompt_carries_the_closed_region_set(self):
        prompt = build_prompt("Keep data in Japan.", catalog)
        assert "asia-northeast1" in prompt
        assert "Keep data in Japan." in prompt
        assert "Never invent a region ID" in prompt


class TestInterpretWithoutKey:
    def test_empty_text_is_rejected_before_any_call(self):
        result = interpret_policy("   ", catalog)
        assert not result.ok
        assert "empty" in result.reason.lower()

    def test_missing_api_key_fails_loudly_with_no_guessed_policy(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        result = interpret_policy("Production data must stay in Japan.", catalog)
        assert not result.ok
        assert result.policy is None
        assert "GEMINI_API_KEY" in result.reason
