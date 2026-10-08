"""Refresh the offline sample in data/sample/ from live sources.

* sec_companyfacts/        real SEC companyfacts for the universe, trimmed to the curated
                           concepts in src/pitlake/conf/metrics.toml (US GAAP and IFRS)
* github_commit_activity/  one real GitHub snapshot per repo in src/pitlake/conf/altdata.toml

    SEC_USER_AGENT="Your Name you@example.com" python scripts/make_sample.py
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import tempfile
from pathlib import Path

from pitlake.config import concept_index, load_universe, sample_dir
from pitlake.extract import SecClient, read_bronze
from pitlake.github import SOURCE, extract_github
from pitlake.transform import TAXONOMIES


def sec(tickers: list[str]) -> None:
    client = SecClient()
    ciks = client.ticker_to_cik()
    keep = set(concept_index())
    out_dir = sample_dir() / "sec_companyfacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    for ticker in tickers:
        payload = client.company_facts(ciks[ticker])
        payload["facts"] = {
            tax: {c: v for c, v in payload["facts"].get(tax, {}).items() if (tax, c) in keep}
            for tax in TAXONOMIES
        }
        payload["_pitlake"] = {"ticker": ticker, "source": "sample"}
        with gzip.open(out_dir / f"{ticker}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), sort_keys=True)
        print(f"SEC {ticker}: {sum(len(v) for v in payload['facts'].values())} concepts")


def github() -> None:
    out_dir = sample_dir() / SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for path in extract_github(None, Path(tmp)):
            payload = read_bronze(path)
            name = f"{payload['ticker']}__{payload['repo'].replace('/', '__')}.json.gz"
            shutil.copy(path, out_dir / name)
            print(f"GitHub {payload['repo']}: {len(payload['weeks'])} weeks")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["sec", "github"])
    args = ap.parse_args()
    if args.only in (None, "sec"):
        sec(load_universe())
    if args.only in (None, "github"):
        github()


if __name__ == "__main__":
    main()
