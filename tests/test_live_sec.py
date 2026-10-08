"""Contract test against the real SEC API. Not part of normal CI (`-m "not live"`).

Runs nightly (.github/workflows/nightly.yml) so that a change in the SEC's response format is
caught by a failing check, not by a broken morning pipeline.
"""

from __future__ import annotations

import os
from datetime import date

import duckdb
import pytest

from pitlake.extract import SecClient
from pitlake.models import OBSERVATION_SCHEMA, to_table
from pitlake.pipeline import register_views_for_batch
from pitlake.quality import check_batch
from pitlake.transform import flatten_company_facts

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("SEC_USER_AGENT"), reason="needs SEC_USER_AGENT"),
]


def test_sec_companyfacts_contract():
    client = SecClient()
    ciks = client.ticker_to_cik()
    assert ciks["NVDA"] == 1045810

    payload = client.company_facts(ciks["NVDA"])
    payload["_pitlake"] = {"ticker": "NVDA"}
    obs = flatten_company_facts(payload)
    concepts = {o.concept for o in obs}
    assert {"NetIncomeLoss", "EarningsPerShareDiluted", "Assets"} <= concepts
    assert max(o.filed for o in obs) > date(2026, 1, 1), "no recent filings: source is stale"

    con = duckdb.connect()
    register_views_for_batch(con)
    report = check_batch(con, to_table(obs, OBSERVATION_SCHEMA))
    assert report.passed, report.summary()
