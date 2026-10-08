"""Alerts for people: failed runs, and changes that need a human decision.

Posts to a Slack incoming webhook when PITLAKE_SLACK_WEBHOOK is set; otherwise it only logs,
so local runs and tests never need credentials.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def send(text: str, client: httpx.Client | None = None) -> bool:
    """Send an alert. Returns True if it was delivered to Slack."""
    url = os.environ.get("PITLAKE_SLACK_WEBHOOK")
    log.warning("ALERT: %s", text)
    if not url:
        return False
    try:
        (client or httpx.Client(timeout=10)).post(url, json={"text": text}).raise_for_status()
        return True
    except httpx.HTTPError:
        # Alerting must never break the pipeline it reports on.
        log.exception("could not deliver alert to Slack")
        return False


def run_summary(results: list[dict]) -> str | None:
    """A message for people, or None if nothing needs attention."""
    lines = []
    quarantined = sum(r.get("quarantined", 0) for r in results)
    restatements = sum(r.get("restatements", 0) for r in results)
    if quarantined:
        lines.append(
            f"{quarantined} change(s) to published numbers are waiting for review: "
            "`pitlake corrections list`."
        )
    if restatements:
        lines.append(
            f"{restatements} published number(s) were revised by companies in this run. "
            "See 'Recent changes' on the pitlake page."
        )
    warnings = sum(r.get("dq_warnings", 0) for r in results)
    if warnings:
        lines.append(
            f"{warnings} data-quality warning(s), e.g. a company with no filing for 4 months: "
            "see the dq_results table."
        )
    return "\n".join(lines) or None
