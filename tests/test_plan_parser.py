from app.plan_parser import parse_plan, zone_to_region


def make_change(address, rtype, actions, before=None, after=None):
    return {
        "address": address,
        "type": rtype,
        "change": {"actions": actions, "before": before, "after": after},
    }


class TestZoneToRegion:
    def test_strips_zone_letter(self):
        assert zone_to_region("asia-northeast1-a") == "asia-northeast1"

    def test_keeps_region_unchanged(self):
        assert zone_to_region("europe-west9") is None or zone_to_region("europe-west9") == "europe-west9"

    def test_garbage_returns_none(self):
        assert zone_to_region("") is None
        assert zone_to_region("a") is None


class TestParsePlan:
    def test_rejects_non_object(self):
        result = parse_plan([1, 2, 3])
        assert result.issues
        assert result.changes == []

    def test_missing_resource_changes(self):
        result = parse_plan({"format_version": "1.2"})
        assert any("resource_changes" in i for i in result.issues)

    def test_empty_resource_changes(self):
        result = parse_plan({"resource_changes": []})
        assert result.issues

    def test_extracts_after_region_from_zone(self):
        plan = {"resource_changes": [
            make_change("google_compute_instance.a", "google_compute_instance",
                        ["create"], None, {"zone": "asia-northeast2-a", "machine_type": "e2-medium"})
        ]}
        result = parse_plan(plan)
        assert result.candidate_regions == ["asia-northeast2"]
        assert result.baseline_region is None
        assert result.changes[0].machine_type == "e2-medium"

    def test_baseline_from_before_zone(self):
        plan = {"resource_changes": [
            make_change("google_compute_instance.a", "google_compute_instance",
                        ["update"], {"zone": "asia-northeast1-a"}, {"zone": "europe-west9-a"})
        ]}
        result = parse_plan(plan)
        assert result.candidate_regions == ["europe-west9"]
        assert result.baseline_region == "asia-northeast1"

    def test_region_field_used_when_no_zone(self):
        plan = {"resource_changes": [
            make_change("google_compute_instance.a", "google_compute_instance",
                        ["create"], None, {"region": "asia-northeast1"})
        ]}
        result = parse_plan(plan)
        assert result.candidate_regions == ["asia-northeast1"]

    def test_delete_is_not_a_candidate(self):
        plan = {"resource_changes": [
            make_change("google_compute_instance.old", "google_compute_instance",
                        ["delete"], {"zone": "asia-northeast1-b"}, None)
        ]}
        result = parse_plan(plan)
        assert result.candidate_regions == []
        assert result.baseline_region == "asia-northeast1"

    def test_noop_and_read_are_ignored(self):
        plan = {"resource_changes": [
            make_change("google_compute_instance.existing", "google_compute_instance",
                        ["no-op"], {"zone": "asia-northeast1-a"}, {"zone": "asia-northeast1-a"}),
            make_change("data.google_compute_zones.avail", "google_compute_zones",
                        ["read"], None, {"region": "asia-northeast1"}),
        ]}
        result = parse_plan(plan)
        assert result.changes == []
        assert result.unsupported == []
        assert result.candidate_regions == []

    def test_unsupported_type_reported_not_silently_skipped(self):
        plan = {"resource_changes": [
            make_change("google_sql_database_instance.db", "google_sql_database_instance",
                        ["create"], None, {"region": "asia-northeast1"})
        ]}
        result = parse_plan(plan)
        assert len(result.unsupported) == 1
        assert result.unsupported[0]["type"] == "google_sql_database_instance"
        assert result.candidate_regions == []

    def test_missing_after_zone_is_an_issue_not_a_guess(self):
        plan = {"resource_changes": [
            make_change("google_compute_instance.a", "google_compute_instance",
                        ["create"], None, {"machine_type": "e2-medium"})
        ]}
        result = parse_plan(plan)
        assert result.candidate_regions == []
        assert any("not guessed" in i or "unknown" in i for i in result.issues)

    def test_missing_actions_is_an_issue(self):
        entry = {"address": "google_compute_instance.a", "type": "google_compute_instance",
                 "change": {"after": {"zone": "asia-northeast1-a"}}}
        result = parse_plan({"resource_changes": [entry]})
        assert result.changes == []
        assert any("actions" in i for i in result.issues)

    def test_non_dict_entry_is_an_issue(self):
        result = parse_plan({"resource_changes": ["oops"]})
        assert result.issues

    def test_missing_type_is_an_issue(self):
        entry = {"address": "x.y", "change": {"actions": ["create"]}}
        result = parse_plan({"resource_changes": [entry]})
        assert result.issues

    def test_duplicate_regions_deduplicated(self):
        plan = {"resource_changes": [
            make_change("google_compute_instance.a", "google_compute_instance",
                        ["create"], None, {"zone": "asia-northeast1-a"}),
            make_change("google_compute_instance.b", "google_compute_instance",
                        ["create"], None, {"zone": "asia-northeast1-b"}),
        ]}
        result = parse_plan(plan)
        assert result.candidate_regions == ["asia-northeast1"]
        assert len(result.changes) == 2
