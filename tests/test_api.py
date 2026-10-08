from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pitlake import pipeline
from pitlake.api import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("api") / "lake"
    pipeline.run("sample", ["NVDA", "AMZN"], lake_root=root)
    return TestClient(create_app(root))


def test_health_reports_last_run(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["last_run"]["status"] == "succeeded"


def test_companies_and_metrics(client):
    companies = {c["ticker"]: c for c in client.get("/api/companies").json()}
    assert set(companies) == {"NVDA", "AMZN"}
    assert (companies["NVDA"]["name"], companies["NVDA"]["cik"]) == ("Nvidia", 1045810)
    assert "eps_diluted" in {m["metric"] for m in client.get("/api/metrics").json()}


def test_as_of_query(client):
    url = "/api/companies/NVDA/metrics/eps_diluted?period_type=annual&as_of={}"
    before = {r["period_end"]: r["value"] for r in client.get(url.format("2024-06-01")).json()}
    after = {r["period_end"]: r["value"] for r in client.get(url.format("2025-03-01")).json()}
    assert (before["2023-01-29"], after["2023-01-29"]) == (1.74, 0.17)


def test_compare_explains_why(client):
    rows = client.get(
        "/api/companies/NVDA/metrics/eps_diluted/compare?before=2024-05-01&after=2024-12-31"
    ).json()
    assert rows
    assert all(r["classification"] == "split_adjustment" for r in rows)
    assert "10-for-1 stock split" in rows[0]["headline"]
    assert "divide them by 10" in rows[0]["guidance"]


def test_changes_feed_filters(client):
    rows = client.get("/api/changes?ticker=AMZN&classification=restatement&limit=5").json()
    assert 0 < len(rows) <= 5
    assert {r["classification"] for r in rows} == {"restatement"}


def test_validation_errors(client):
    assert client.get("/api/companies/NVDA/metrics/not_a_metric").status_code == 404
    assert client.get("/api/changes?classification=bogus").status_code == 422
    assert (
        client.get(
            "/api/companies/NVDA/metrics/revenue/compare?before=2024-01-01&after=2023-01-01"
        ).status_code
        == 422
    )


def test_developer_activity(client):
    rows = client.get("/api/companies/NVDA/developer-activity").json()
    assert {r["repo"] for r in rows} == {"NVIDIA/cccl", "NVIDIA/cutlass"}
    assert client.get("/api/companies/NVDA/developer-activity?as_of=2020-01-01").json() == []


def test_review_queue_shows_amazon_tagging_error(client):
    pending = client.get("/api/corrections").json()
    assert pending and {c["reason_code"] for c in pending} == {"suspected_wrong_period"}


def test_analyst_page_is_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "How to read this page" in resp.text


def test_api_notices_a_recreated_lake(tmp_path):
    """A rebuilt lake can repeat version numbers; the API must not serve the old snapshot."""
    from pitlake.lake import Lake

    root = tmp_path / "lake"
    pipeline.run("sample", ["HNGE"], lake_root=root)
    api = TestClient(create_app(root))
    assert {c["ticker"] for c in api.get("/api/companies").json()} == {"HNGE"}
    import shutil

    shutil.rmtree(root)
    pipeline.run("sample", ["SIMO"], lake_root=root)
    assert Lake(root).version("facts_history") >= 0
    assert {c["ticker"] for c in api.get("/api/companies").json()} == {"SIMO"}
