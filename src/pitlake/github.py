"""Alternative data: developer activity on GitHub.

Each run lands GitHub's weekly commit counts (last 52 weeks) per repository as an immutable
bronze snapshot, then flattens it into the same Observation model as SEC data, so the same
versioning rules, data-quality gates and as-of queries apply.

Point-in-time: the "filing" is the collection itself. A weekly count is known from the day
it was collected (`filed` = collection date). The full year returned on the first run is
therefore known only from that day: no backtest can use history we did not have yet.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from pitlake.config import config_dir, display_names
from pitlake.models import Observation

log = logging.getLogger(__name__)

API = "https://api.github.com"
SOURCE = "github_commit_activity"
TAXONOMY = "github"
UNIT = "commits"
FORM = "GitHub"


def load_repos() -> dict[str, list[str]]:
    raw = tomllib.loads((config_dir() / "altdata.toml").read_text())
    return {t: list(repos) for t, repos in raw["github"]["repos"].items()}


class GitHubClient:
    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        token = token or os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:  # 5,000 requests/hour instead of 60
            headers["Authorization"] = f"Bearer {token}"
        # follow_redirects: GitHub answers renamed or transferred repositories with a 301.
        self._client = client or httpx.Client(headers=headers, timeout=30, follow_redirects=True)

    def commit_activity(self, repo: str, retries: int = 6) -> list[dict]:
        """Weekly commit totals for the last 52 weeks: [{"week": unix_ts, "total": n, ...}]."""
        url = f"{API}/repos/{repo}/stats/commit_activity"
        for attempt in range(retries):
            resp = self._client.get(url)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 204:  # empty repository
                return []
            if resp.status_code == 202:  # GitHub is computing the statistics; ask again
                time.sleep(min(2**attempt, 30))
                continue
            if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
                raise RuntimeError(
                    f"GitHub rate limit reached while fetching {repo}. Set GITHUB_TOKEN "
                    "for 5,000 requests/hour."
                )
            resp.raise_for_status()
        raise RuntimeError(f"GitHub is still computing statistics for {repo}; retry later")


def _bronze_dir(bronze_root: Path, repo: str) -> Path:
    return bronze_root / SOURCE / f"repo={repo.replace('/', '__')}"


def extract_github(
    tickers: list[str] | None,
    bronze_root: Path,
    client: GitHubClient | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """Land one snapshot per repo per day. A re-run on the same day reuses that day's
    snapshot, so each day has exactly one vintage."""
    now = now or datetime.now(UTC)
    repos = load_repos()
    wanted = {t.upper() for t in tickers} if tickers else set(repos)
    client = client or GitHubClient()
    paths = []
    for ticker in sorted(wanted & set(repos)):
        for repo in repos[ticker]:
            folder = _bronze_dir(bronze_root, repo)
            today = sorted(folder.glob(f"fetched_at={now:%Y%m%d}T*.json.gz"))
            if today:
                paths.append(today[-1])
                continue
            payload = {
                "source": SOURCE,
                "ticker": ticker,
                "repo": repo,
                "fetched_at": now.isoformat(),
                "weeks": client.commit_activity(repo),
            }
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"fetched_at={now:%Y%m%dT%H%M%SZ}.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            paths.append(path)
            log.info("Landed GitHub activity for %s (%s)", repo, ticker)
    return paths


def flatten_commit_activity(payload: dict) -> list[Observation]:
    """One Observation per complete week. The current, unfinished week is skipped."""
    collected = datetime.fromisoformat(payload["fetched_at"]).date()
    ticker = payload["ticker"]
    out = []
    for w in payload["weeks"]:
        start = datetime.fromtimestamp(w["week"], UTC).date()  # GitHub weeks start on Sunday
        end = start + timedelta(days=6)
        if end >= collected:
            continue
        out.append(
            Observation(
                ticker=ticker,
                cik=None,
                entity_name=display_names().get(ticker, ticker),
                taxonomy=TAXONOMY,
                concept=payload["repo"],
                unit=UNIT,
                period_start=start,
                period_end=end,
                value=float(w["total"]),
                accn=vintage_id(collected),
                form=FORM,
                fiscal_year=None,
                fiscal_period=None,
                filed=collected,
            )
        )
    return out


def vintage_id(collected: date) -> str:
    return f"gh-{collected.isoformat()}"
