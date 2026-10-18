from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    body = client.get("/api/healthz").json()
    assert body["status"] == "ok"
    assert "gemini_configured" in body


def test_carbon_data_endpoint():
    body = client.get("/api/carbon-data").json()
    assert body["metadata"]["data_year"] == 2025
    assert "cloud.google.com" in body["source"]


def test_samples_endpoint_returns_every_scene():
    body = client.get("/api/samples").json()
    assert len(body["samples"]) == 4
    for sample in body["samples"]:
        assert {"id", "title", "description", "plan", "policy"} <= set(sample)


def test_prose_sample_carries_policy_text():
    samples = client.get("/api/samples").json()["samples"]
    prose = next(s for s in samples if s["id"] == "4-natural-language-policy")
    assert prose["policy_text"].strip()


def test_agents_endpoint_lists_the_pipeline():
    body = client.get("/api/agents").json()
    names = [a["name"] for a in body["agents"]]
    assert "policy-gate" in names and "review-writer" in names
    assert {a["engine"] for a in body["agents"]} == {"deterministic", "gemini"}


def test_review_requires_a_policy_of_some_kind():
    res = client.post("/api/review", json={"plan": {"resource_changes": []}})
    assert res.status_code == 422
    assert "policy" in res.json()["detail"].lower()


def test_review_endpoint_end_to_end():
    samples = client.get("/api/samples").json()["samples"]
    scene2 = next(s for s in samples if s["id"] == "2-compare-allowed")
    res = client.post("/api/review", json={"plan": scene2["plan"], "policy": scene2["policy"]})
    assert res.status_code == 200
    body = res.json()
    assert body["verdict"] == "ok"
    assert body["comparison"][0]["region"] == "asia-northeast2"
    assert body["elapsed_ms"] >= 0


def test_review_endpoint_blocked_scene():
    samples = client.get("/api/samples").json()["samples"]
    scene1 = next(s for s in samples if s["id"] == "1-blocked-greener-region")
    res = client.post("/api/review", json={"plan": scene1["plan"], "policy": scene1["policy"]})
    assert res.json()["verdict"] == "blocked"


def test_review_endpoint_bad_policy_422():
    res = client.post("/api/review", json={"plan": {}, "policy": {"allowed_regions": []}})
    assert res.status_code == 422
    assert "detail" in res.json()


def test_index_served():
    res = client.get("/")
    assert res.status_code == 200
    assert "Policy-Compliant Carbon Reviewer" in res.text


def test_static_assets_served():
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_security_headers():
    res = client.get("/api/healthz")
    assert res.headers["X-Content-Type-Options"] == "nosniff"


def test_review_is_timestamped_for_the_audit_record():
    samples = client.get("/api/samples").json()["samples"]
    scene = samples[0]
    body = client.post(
        "/api/review", json={"plan": scene["plan"], "policy": scene["policy"]}
    ).json()
    assert body["reviewed_at"].endswith("+00:00")
    from datetime import datetime

    datetime.fromisoformat(body["reviewed_at"])  # parses, so it is a real stamp
