from app.carbon import CarbonCatalog
from app.impact import (
    ASSUMED_PUE,
    HOURS_PER_YEAR,
    WATTS_PER_VCPU,
    annual_kg_co2e,
    annual_kwh,
    estimate_plan_impact,
    vcpu_count,
)
from app.plan_parser import ResourceChange

catalog = CarbonCatalog()


def change(address="a", machine_type="e2-standard-4", before="asia-northeast1",
           after="asia-northeast2", actions=None):
    return ResourceChange(
        address=address,
        resource_type="google_compute_instance",
        actions=actions or ["update"],
        before_zone=None,
        after_zone=None,
        before_region=before,
        after_region=after,
        machine_type=machine_type,
        supported=True,
    )


class TestVcpuCount:
    def test_standard_shapes(self):
        assert vcpu_count("e2-standard-4") == 4
        assert vcpu_count("n2-standard-8") == 8
        assert vcpu_count("n2d-highmem-16") == 16
        assert vcpu_count("c3-highcpu-22") == 22

    def test_suffixed_shape(self):
        assert vcpu_count("c3-standard-8-lssd") == 8

    def test_shared_core(self):
        assert vcpu_count("e2-micro") == 0.25
        assert vcpu_count("f1-micro") == 0.2
        assert vcpu_count("e2-medium") == 1.0

    def test_custom_shapes(self):
        assert vcpu_count("custom-4-8192") == 4
        assert vcpu_count("n2-custom-8-16384") == 8

    def test_case_and_whitespace(self):
        assert vcpu_count("  E2-STANDARD-4 ") == 4

    def test_unknown_returns_none_never_a_default(self):
        assert vcpu_count("totally-made-up") is None
        assert vcpu_count("") is None
        assert vcpu_count(None) is None
        assert vcpu_count(42) is None


class TestEnergyMath:
    def test_annual_kwh_matches_the_documented_formula(self):
        expected = 4 * WATTS_PER_VCPU / 1000 * HOURS_PER_YEAR * ASSUMED_PUE
        assert annual_kwh(4) == expected

    def test_emissions_scale_with_grid_intensity(self):
        clean = annual_kg_co2e(4, 16)     # Paris
        dirty = annual_kg_co2e(4, 449)    # Tokyo
        assert dirty > clean
        assert round(dirty / clean, 1) == round(449 / 16, 1)

    def test_zero_intensity_is_zero(self):
        assert annual_kg_co2e(8, 0) == 0


class TestPlanImpact:
    def test_move_to_cleaner_region_is_a_negative_delta(self):
        est = estimate_plan_impact([change()], catalog)
        assert est.estimated_count == 1
        assert est.total_delta_kg_co2e < 0
        assert est.total_baseline_kg_co2e > est.total_target_kg_co2e

    def test_recommended_region_overrides_the_plan_target(self):
        est = estimate_plan_impact([change()], catalog, recommended_region="europe-west9")
        assert est.resources[0].target_region == "europe-west9"

    def test_blocked_plan_target_is_never_costed(self):
        """The core guarantee: no 'saving' is reported for a forbidden move."""
        est = estimate_plan_impact(
            [change(after="europe-west9")], catalog, allow_plan_target=False
        )
        assert est.total_target_kg_co2e is None
        assert est.total_delta_kg_co2e is None
        assert est.total_baseline_kg_co2e is not None  # current footprint still known
        assert "no policy-approved target" in est.resources[0].note.lower()

    def test_unknown_machine_type_is_reported_not_guessed(self):
        est = estimate_plan_impact([change(machine_type="mystery-shape")], catalog)
        assert est.estimated_count == 0
        assert est.unestimated_count == 1
        assert est.total_delta_kg_co2e is None
        assert "no vcpu count" in est.resources[0].note.lower()

    def test_unknown_region_is_reported_not_guessed(self):
        est = estimate_plan_impact([change(after="atlantis-central1")], catalog)
        assert est.resources[0].target_kg_co2e is None
        assert "no official carbon data" in est.resources[0].note.lower()

    def test_deletes_are_skipped(self):
        est = estimate_plan_impact([change(actions=["delete"])], catalog)
        assert est.resources == []

    def test_unsupported_changes_are_skipped(self):
        c = change()
        c.supported = False
        assert estimate_plan_impact([c], catalog).resources == []

    def test_totals_add_up_across_resources(self):
        changes = [change(address="a"), change(address="b")]
        est = estimate_plan_impact(changes, catalog)
        assert est.estimated_count == 2
        assert est.total_target_kg_co2e == round(
            sum(r.target_kg_co2e for r in est.resources), 1
        )

    def test_assumptions_are_always_attached(self):
        est = estimate_plan_impact([change()], catalog)
        assert est.assumptions
        assert any("vCPU" in a for a in est.assumptions)
