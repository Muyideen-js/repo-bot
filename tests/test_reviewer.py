from datetime import datetime, timedelta, timezone

import pytest

from app.services import pr_reviewer
from app.services.pr_reviewer import _recently_updated


def test_recent_pr_waits_for_ci_to_appear():
    updated = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    assert _recently_updated({"updated_at": updated}) is True


def test_old_pr_can_proceed_without_ci():
    updated = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert _recently_updated({"updated_at": updated}) is False


@pytest.mark.asyncio
async def test_dirty_approved_pr_is_repaired(monkeypatch):
    async def dirty(*args, **kwargs):
        return "dirty"

    async def no_repairs(*args, **kwargs):
        return 0

    async def repair(**kwargs):
        return "a" * 40

    monkeypatch.setattr(pr_reviewer.gh, "get_pr_merge_state", dirty)
    monkeypatch.setattr(pr_reviewer, "_repair_count", no_repairs)
    monkeypatch.setattr(pr_reviewer, "repair_pull_request_conflicts", repair)

    decision, summary = await pr_reviewer._repair_conflict_if_safe(
        token="token",
        repo_full_name="owner/repo",
        pr_number=7,
        expected_sha="b" * 40,
        issue={"title": "Issue", "body": "Body"},
        pr={"title": "PR", "body": "Body"},
        db=object(),
        current_summary="Approved",
    )
    assert decision == "CONFLICT_REPAIRED"
    assert "waiting for fresh CI" in summary


@pytest.mark.asyncio
async def test_non_conflict_merge_block_is_not_repaired(monkeypatch):
    async def blocked(*args, **kwargs):
        return "blocked"

    async def unexpected_repair(**kwargs):
        pytest.fail("A branch-protection block must not trigger conflict repair")

    monkeypatch.setattr(pr_reviewer.gh, "get_pr_merge_state", blocked)
    monkeypatch.setattr(pr_reviewer, "repair_pull_request_conflicts", unexpected_repair)

    decision, summary = await pr_reviewer._repair_conflict_if_safe(
        token="token",
        repo_full_name="owner/repo",
        pr_number=7,
        expected_sha="b" * 40,
        issue={},
        pr={},
        db=object(),
        current_summary="Approved",
    )
    assert (decision, summary) == ("MERGE_BLOCKED", "Approved")


@pytest.mark.asyncio
async def test_conflict_repair_stops_after_configured_limit(monkeypatch):
    async def dirty(*args, **kwargs):
        return "dirty"

    async def two_repairs(*args, **kwargs):
        return 2

    monkeypatch.setattr(pr_reviewer.gh, "get_pr_merge_state", dirty)
    monkeypatch.setattr(pr_reviewer, "_repair_count", two_repairs)
    monkeypatch.setenv("AUTO_RESOLVE_MAX_ATTEMPTS", "2")

    decision, summary = await pr_reviewer._repair_conflict_if_safe(
        token="token",
        repo_full_name="owner/repo",
        pr_number=7,
        expected_sha="b" * 40,
        issue={},
        pr={},
        db=object(),
        current_summary="Approved",
    )
    assert decision == "MERGE_BLOCKED"
    assert "after 2 automatic repairs" in summary
