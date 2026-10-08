"""SEC client against a mocked transport (CI never calls the real SEC) and a CLI smoke test."""

from __future__ import annotations

import httpx
import pytest

from pitlake import extract
from pitlake.cli import main
from pitlake.extract import SecClient, extract_sec, read_bronze


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(extract.time, "sleep", lambda s: None)


def mock_client(handler) -> SecClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "t t@t"})
    return SecClient(user_agent="t t@t", client=http)


def test_user_agent_is_required(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="User-Agent"):
        SecClient()


def test_retries_on_rate_limit_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, json={"ok": 1})

    assert mock_client(handler).get_json("https://data.sec.gov/x") == {"ok": 1}
    assert calls["n"] == 3


def test_gives_up_on_client_errors():
    client = mock_client(lambda r: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        client.get_json("https://data.sec.gov/x")


def test_extract_lands_raw_payload_in_bronze(tmp_path):
    def handler(request):
        if request.url.path.endswith("company_tickers.json"):
            return httpx.Response(
                200, json={"0": {"ticker": "ACME", "cik_str": 1, "title": "Acme"}}
            )
        assert request.url.path.endswith("CIK0000000001.json")
        return httpx.Response(200, json={"cik": 1, "entityName": "Acme", "facts": {}})

    paths = extract_sec(["ACME", "NOPE"], tmp_path, client=mock_client(handler))
    [path] = paths  # the unknown ticker is skipped, not fatal
    assert "ticker=ACME" in str(path)
    payload = read_bronze(path)
    assert payload["entityName"] == "Acme"
    assert payload["_pitlake"]["ticker"] == "ACME"


def test_cli_ingest_and_query(tmp_path, capsys):
    lake = str(tmp_path / "lake")
    assert main(["--lake", lake, "ingest", "NVDA"]) == 0
    assert main(["--lake", lake, "asof", "NVDA", "eps_diluted", "--as-of", "2024-06-01"]) == 0
    assert "1.74" in capsys.readouterr().out
    assert main(["--lake", lake, "changes", "--ticker", "NVDA", "--limit", "1"]) == 0
    assert main(["--lake", lake, "history", "NVDA", "eps_diluted", "2023-01-29"]) == 0
    assert main(["--lake", lake, "study"]) == 0
    assert "Does point-in-time data change the answer?" in capsys.readouterr().out
    assert main(["--lake", lake, "corrections", "list"]) == 0
    assert main(["--lake", lake, "runs"]) == 0
    assert main(["--lake", lake, "corrections", "approve", "nope", "--reviewer", "x"]) == 2
