import pytest

from app.carbon import CarbonCatalog
from app.policy import Policy, evaluate_region

catalog = CarbonCatalog()
JP_POLICY = {
    "name": "Sample Japan data residency",
    "allowed_regions": ["asia-northeast1", "asia-northeast2"],
    "data_residency": {"jurisdiction": "JP", "label": "Japan"},
}


class TestPolicyParsing:
    def test_valid_policy(self):
        policy = Policy.from_dict(JP_POLICY)
        assert policy.name == "Sample Japan data residency"
        assert policy.residency_jurisdiction == "JP"

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError):
            Policy.from_dict(["not", "a", "dict"])

    def test_rejects_missing_name(self):
        with pytest.raises(ValueError):
            Policy.from_dict({"allowed_regions": ["asia-northeast1"]})

    def test_rejects_empty_allowed_regions(self):
        with pytest.raises(ValueError):
            Policy.from_dict({"name": "x", "allowed_regions": []})

    def test_rejects_bad_allowed_regions_type(self):
        with pytest.raises(ValueError):
            Policy.from_dict({"name": "x", "allowed_regions": "asia-northeast1"})

    def test_rejects_bad_residency(self):
        with pytest.raises(ValueError):
            Policy.from_dict({
                "name": "x",
                "allowed_regions": ["asia-northeast1"],
                "data_residency": {"country": "JP"},  # wrong key
            })

    def test_policy_without_residency(self):
        policy = Policy.from_dict({"name": "x", "allowed_regions": ["asia-northeast1"]})
        assert policy.residency_jurisdiction is None


class TestGate:
    def test_allowed_region_passes(self):
        policy = Policy.from_dict(JP_POLICY)
        result = evaluate_region(policy, "asia-northeast2", catalog)
        assert result.allowed
        assert result.reasons == []
        assert result.known_region

    def test_greener_region_outside_list_is_blocked(self):
        # The demo hook: Paris (16 gCO2eq/kWh) is much cleaner than Tokyo (449)
        # and still blocked.
        policy = Policy.from_dict(JP_POLICY)
        result = evaluate_region(policy, "europe-west9", catalog)
        assert not result.allowed
        assert any("not in the allowed region list" in r for r in result.reasons)

    def test_residency_violation_reason_mentions_jurisdiction(self):
        policy = Policy.from_dict(JP_POLICY)
        result = evaluate_region(policy, "europe-west9", catalog)
        assert any("FR" in r and "Japan" in r for r in result.reasons)

    def test_unknown_region_fails_residency_check(self):
        policy = Policy.from_dict(JP_POLICY)
        result = evaluate_region(policy, "atlantis-central1", catalog)
        assert not result.allowed
        assert not result.known_region
        assert any("cannot be verified" in r for r in result.reasons)

    def test_no_residency_policy_allows_known_foreign_region(self):
        policy = Policy.from_dict({
            "name": "open", "allowed_regions": ["europe-west9"],
        })
        result = evaluate_region(policy, "europe-west9", catalog)
        assert result.allowed

    def test_residency_jurisdiction_uppercased(self):
        policy = Policy.from_dict({
            "name": "x",
            "allowed_regions": ["asia-northeast1"],
            "data_residency": {"jurisdiction": "jp"},
        })
        assert policy.residency_jurisdiction == "JP"
