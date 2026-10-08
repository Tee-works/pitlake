"""Contract test against the real GitHub API. Not part of normal CI (`-m "not live"`)."""

from __future__ import annotations

import pytest

from pitlake.github import GitHubClient

pytestmark = pytest.mark.live


def test_commit_activity_contract():
    weeks = GitHubClient().commit_activity("NVIDIA/cutlass")
    assert len(weeks) == 52
    assert {"week", "total", "days"} <= set(weeks[0])
    assert all(w["total"] >= 0 for w in weeks)
