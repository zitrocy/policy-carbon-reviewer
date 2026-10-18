from app.carbon import CarbonCatalog

catalog = CarbonCatalog()


class TestCatalog:
    def test_official_2025_values_spot_check(self):
        # Values from Google's official region carbon page (2025 annual data).
        assert catalog.get("asia-northeast1").grid_carbon_intensity == 449  # Tokyo
        assert catalog.get("asia-northeast2").grid_carbon_intensity == 299  # Osaka
        assert catalog.get("europe-west9").grid_carbon_intensity == 16      # Paris
        assert catalog.get("asia-northeast2").cfe_pct == 45
        assert catalog.get("europe-west9").cfe_pct == 96

    def test_unknown_region(self):
        assert catalog.get("atlantis-central1") is None
        assert not catalog.known("atlantis-central1")

    def test_country_mapping(self):
        assert catalog.get("asia-northeast1").country == "JP"
        assert catalog.get("europe-west9").country == "FR"

    def test_low_carbon_rule(self):
        assert catalog.get("europe-west9").low_carbon       # CFE 96%
        assert catalog.get("us-west1").low_carbon           # 98 gCO2eq/kWh, CFE 83
        assert not catalog.get("asia-northeast1").low_carbon  # Tokyo 449, CFE 23

    def test_metadata_present(self):
        meta = catalog.metadata
        assert meta["data_year"] == 2025
        assert "cloud.google.com" in meta["source_url"]


class TestCompare:
    def test_sorted_cleanest_first(self):
        rows = catalog.compare(["asia-northeast1", "asia-northeast2"], "asia-northeast1")
        assert [r.region for r in rows] == ["asia-northeast2", "asia-northeast1"]

    def test_delta_vs_baseline(self):
        rows = catalog.compare(["asia-northeast2"], "asia-northeast1")
        # (299 - 449) / 449 = -33.4%
        assert rows[0].delta_pct_vs_baseline == -33.4

    def test_baseline_itself_has_zero_delta(self):
        rows = catalog.compare(["asia-northeast1"], "asia-northeast1")
        assert rows[0].delta_pct_vs_baseline == 0

    def test_no_baseline_means_no_delta(self):
        rows = catalog.compare(["asia-northeast2"], None)
        assert rows[0].delta_pct_vs_baseline is None

    def test_unknown_regions_skipped(self):
        rows = catalog.compare(["atlantis-central1", "asia-northeast1"], None)
        assert [r.region for r in rows] == ["asia-northeast1"]

    def test_empty_input(self):
        assert catalog.compare([], "asia-northeast1") == []
