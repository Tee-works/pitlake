from __future__ import annotations

import httpx

from pitlake import notify


def test_without_webhook_alerts_are_only_logged(monkeypatch, caplog):
    monkeypatch.delenv("PITLAKE_SLACK_WEBHOOK", raising=False)
    assert notify.send("hello") is False
    assert "ALERT: hello" in caplog.text


def test_posts_to_slack_when_configured(monkeypatch):
    monkeypatch.setenv("PITLAKE_SLACK_WEBHOOK", "https://hooks.slack.test/x")
    sent = []

    def handler(request):
        sent.append(request.content)
        return httpx.Response(200)

    assert notify.send("hi", httpx.Client(transport=httpx.MockTransport(handler))) is True
    assert sent == [b'{"text":"hi"}']


def test_a_broken_webhook_never_breaks_the_pipeline(monkeypatch):
    monkeypatch.setenv("PITLAKE_SLACK_WEBHOOK", "https://hooks.slack.test/x")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert notify.send("hi", client) is False


def test_run_summary():
    assert notify.run_summary([{"quarantined": 0, "restatements": 0}]) is None
    msg = notify.run_summary([{"quarantined": 2, "restatements": 0}, {"restatements": 3}])
    assert "2 change(s) to published numbers are waiting for review" in msg
    assert "3 published number(s) were revised" in msg
