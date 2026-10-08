"""Extract: fetch SEC companyfacts and land the raw JSON untouched in the bronze layer.

Bronze files are immutable and timestamped, so any published number can be traced back
to the exact bytes we received, and the pipeline can be replayed from bronze.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# SEC fair-access policy: max 10 requests/second and a User-Agent naming a contact.
_MIN_INTERVAL_S = 0.15


class SecClient:
    def __init__(self, user_agent: str | None = None, client: httpx.Client | None = None):
        user_agent = user_agent or os.environ.get("SEC_USER_AGENT")
        if not user_agent:
            raise RuntimeError(
                "SEC requires a User-Agent with contact details. "
                "Set SEC_USER_AGENT, e.g. 'Jane Doe jane@example.com'."
            )
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"}, timeout=30
        )
        self._last_request = 0.0

    def get_json(self, url: str, retries: int = 4) -> dict:
        for attempt in range(retries + 1):
            wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            resp = self._client.get(url)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                backoff = 2**attempt
                log.warning("SEC %s for %s, retrying in %ss", resp.status_code, url, backoff)
                time.sleep(backoff)
                continue
            resp.raise_for_status()
        raise RuntimeError(f"unreachable: {url}")

    def ticker_to_cik(self) -> dict[str, int]:
        data = self.get_json(TICKERS_URL)
        return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}

    def company_facts(self, cik: int) -> dict:
        return self.get_json(FACTS_URL.format(cik=cik))


def write_bronze(bronze_root: Path, ticker: str, payload: dict, fetched_at: datetime) -> Path:
    stamp = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    path = bronze_root / "sec_companyfacts" / f"ticker={ticker}" / f"fetched_at={stamp}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    return path


def extract_sec(
    tickers: list[str], bronze_root: Path, client: SecClient | None = None
) -> list[Path]:
    client = client or SecClient()
    ciks = client.ticker_to_cik()
    fetched_at = datetime.now(UTC)
    paths = []
    for ticker in tickers:
        cik = ciks.get(ticker.upper())
        if cik is None:
            log.error("Unknown ticker %s, skipping", ticker)
            continue
        payload = client.company_facts(cik)
        payload["_pitlake"] = {"ticker": ticker.upper(), "fetched_at": fetched_at.isoformat()}
        paths.append(write_bronze(bronze_root, ticker.upper(), payload, fetched_at))
        log.info("Landed %s (CIK %s)", ticker, cik)
    return paths


def sample_files(sample_root: Path, tickers: list[str] | None = None) -> list[Path]:
    """Offline snapshots of real data shipped with the repo (see scripts/make_sample.py).

    Files are named after their ticker: `NVDA.json.gz`, `NVDA__NVIDIA__cccl.json.gz`.
    """
    files = sorted(sample_root.glob("**/*.json.gz"))
    if tickers:
        wanted = {t.upper() for t in tickers}
        files = [f for f in files if f.name.split(".")[0].split("__")[0] in wanted]
    return files


def read_bronze(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)
