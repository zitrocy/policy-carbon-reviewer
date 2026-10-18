import json

from app.gemini import (
    build_prompt,
    generate_review,
    offline_review,
    render_comment,
    validate_review,
)

ROWS = [
    {"region": "asia-northeast2", "location": "Osaka", "cfe_pct": 45,
     "grid_carbon_intensity": 299, "low_carbon": False, "delta_pct_vs_baseline": -33.4},
    {"region": "asia-northeast1", "location": "Tokyo", "cfe_pct": 23,
     "grid_carbon_intensity": 449, "low_carbon": False, "delta_pct_vs_baseline": 0},
]
BLOCKED = [{"region": "europe-west9", "reasons": ["Region 'europe-west9' is not in the allowed region list."]}]
SOURCE = "Google Cloud official 2025 annual data (test)"
ALLOWED_IDS = [r["region"] for r in ROWS]


class TestPrompt:
    def test_prompt_contains_only_allowed_candidates(self):
        prompt = build_prompt("P", "asia-northeast1", ROWS, BLOCKED, SOURCE)
        assert "asia-northeast2" in prompt
        assert "asia-northeast1" in prompt
        # Blocked region appears only in the blocked section, labeled as never-recommend.
        blocked_section = prompt.split("BLOCKED BY POLICY")[1]
        assert "europe-west9" in blocked_section
        assert "never recommend" in blocked_section.lower()

    def test_prompt_carries_computed_numbers(self):
        prompt = build_prompt("P", "asia-northeast1", ROWS, [], SOURCE)
        assert "299" in prompt and "449" in prompt
        assert "-33.4%" in prompt

    def test_prompt_without_baseline(self):
        prompt = build_prompt("P", None, ROWS, [], SOURCE)
        assert "new workload" in prompt


class TestValidation:
    def good(self):
        return {
            "recommended_region": "asia-northeast2",
            "headline": "Osaka is the cleanest allowed region.",
            "rationale": ["299 vs 449 gCO2eq/kWh."],
            "tradeoffs": ["Latency needs a separate check."],
        }

    def test_accepts_valid(self):
        ok, reason = validate_review(self.good(), ALLOWED_IDS)
        assert ok, reason

    def test_rejects_region_outside_allowed(self):
        bad = self.good()
        bad["recommended_region"] = "europe-west9"
        ok, reason = validate_review(bad, ALLOWED_IDS)
        assert not ok and "not an allowed candidate" in reason

    def test_rejects_allowed_but_not_cleanest_region(self):
        bad = self.good()
        bad["recommended_region"] = "asia-northeast1"  # allowed, but dirtier than asia-northeast2
        ok, reason = validate_review(bad, ALLOWED_IDS)
        assert not ok and "not the cleanest" in reason

    def test_rejects_non_dict(self):
        ok, _ = validate_review("text", ALLOWED_IDS)
        assert not ok

    def test_rejects_missing_field(self):
        bad = self.good()
        del bad["tradeoffs"]
        ok, reason = validate_review(bad, ALLOWED_IDS)
        assert not ok and "tradeoffs" in reason

    def test_rejects_empty_headline(self):
        bad = self.good()
        bad["headline"] = "   "
        ok, _ = validate_review(bad, ALLOWED_IDS)
        assert not ok

    def test_rejects_empty_rationale(self):
        bad = self.good()
        bad["rationale"] = []
        ok, _ = validate_review(bad, ALLOWED_IDS)
        assert not ok

    def test_rejects_non_string_bullets(self):
        bad = self.good()
        bad["rationale"] = [42]
        ok, _ = validate_review(bad, ALLOWED_IDS)
        assert not ok


class TestOfflineReview:
    def test_offline_status_and_labeling(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        review = generate_review("P", "asia-northeast1", ROWS, BLOCKED, SOURCE)
        assert review.status == "offline"
        assert "not model output" in review.note
        assert review.model is None

    def test_offline_picks_cleanest_allowed(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        review = offline_review(ROWS, "asia-northeast1", SOURCE)
        assert "`asia-northeast2`" in review.comment_markdown
        assert "299" in review.comment_markdown

    def test_offline_comment_has_table_and_source(self):
        review = offline_review(ROWS, "asia-northeast1", SOURCE)
        assert "| Region |" in review.comment_markdown
        assert SOURCE in review.comment_markdown
        assert "baseline" in review.comment_markdown.lower()

    def test_no_candidates_returns_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        review = generate_review("P", None, [], [], SOURCE)
        assert review.status == "error"


class TestRenderComment:
    def test_markdown_shape(self):
        review = {
            "recommended_region": "asia-northeast2",
            "headline": "  Move to Osaka. ",
            "rationale": [" Cleaner grid. "],
            "tradeoffs": [],
        }
        comment = render_comment(review, ROWS, "asia-northeast1", SOURCE)
        assert comment.startswith("**Move to Osaka.**")
        assert "- Cleaner grid." in comment
        assert "`asia-northeast2`" in comment
        # Trade-off section omitted when empty
        assert "Trade-offs" not in comment


class TestOfflineBaselineRationale:
    def test_delta_uses_best_row_not_baseline_row(self):
        rows = ROWS + [{"region": "asia-northeast1", "location": "Tokyo", "cfe_pct": 23,
                        "grid_carbon_intensity": 449, "low_carbon": False,
                        "delta_pct_vs_baseline": 0}]
        review = offline_review(rows, "asia-northeast1", SOURCE)
        assert "-33.4%" in review.comment_markdown
        assert "+0.0%" not in review.comment_markdown

    def test_staying_in_baseline_region(self):
        only_tokyo = [{"region": "asia-northeast1", "location": "Tokyo", "cfe_pct": 23,
                       "grid_carbon_intensity": 449, "low_carbon": False,
                       "delta_pct_vs_baseline": 0}]
        review = offline_review(only_tokyo, "asia-northeast1", SOURCE)
        assert "stays in its current region" in review.comment_markdown
