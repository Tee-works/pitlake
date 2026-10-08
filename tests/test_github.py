"""Alternative data: GitHub developer activity (mocked HTTP; CI never calls GitHub)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from pitlake import github
from pitlake.github import GitHubClient, extract_github, flatten_commit_activity

SUNDAY = int(datetime(2026, 9, 27, tzinfo=UTC).timestamp())
WEEK = 7 * 86400


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(github.time, "sleep", lambda s: None)


def client(handler) -> GitHubClient:
    return GitHubClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


def payload(fetched: str, weeks: list[tuple[int, int]]) -> dict:
    return {
        "source": github.SOURCE,
        "ticker": "NVDA",
        "repo": "NVIDIA/cccl",
        "fetched_at": fetched,
        "weeks": [{"week": w, "total": t, "days": [0] * 7} for w, t in weeks],
    }


def test_complete_weeks_become_observations_known_from_collection():
    obs = flatten_commit_activity(
        payload(
            "2026-10-08T06:00:00+00:00", [(SUNDAY - WEEK, 40), (SUNDAY, 55), (SUNDAY + WEEK, 9)]
        )
    )
    # The week starting 4 Oct is still running on 8 Oct, so it is skipped.
    assert [(o.period_start, o.period_end, o.value) for o in obs] == [
        (date(2026, 9, 20), date(2026, 9, 26), 40.0),
        (date(2026, 9, 27), date(2026, 10, 3), 55.0),
    ]
    assert {(o.filed, o.accn, o.taxonomy, o.unit) for o in obs} == {
        (date(2026, 10, 8), "gh-2026-10-08", "github", "commits")
    }
    assert obs[0].entity_name == "Nvidia"


def test_waits_while_github_computes_statistics():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(202)
        return httpx.Response(200, json=[{"week": SUNDAY, "total": 5, "days": [0] * 7}])

    assert client(handler).commit_activity("NVIDIA/cccl")[0]["total"] == 5
    assert calls["n"] == 3


def test_rate_limit_explains_how_to_fix_it():
    limited = client(lambda r: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        limited.commit_activity("NVIDIA/cccl")


def test_one_snapshot_per_repo_per_day(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[{"week": SUNDAY, "total": 5, "days": [0] * 7}])

    morning = datetime(2026, 10, 8, 6, tzinfo=UTC)
    first = extract_github(["NVDA"], tmp_path, client(handler), now=morning)
    again = extract_github(["NVDA"], tmp_path, client(handler), now=morning.replace(hour=9))
    assert first == again, "a re-run on the same day replays the same vintage"
    assert len(calls) == 2  # NVDA has two repos; the second run made no requests
    assert "repo=NVIDIA__cccl" in str(first[0])
