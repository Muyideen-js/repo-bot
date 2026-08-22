from datetime import datetime, timedelta, timezone

from app.services.pr_reviewer import _recently_updated


def test_recent_pr_waits_for_ci_to_appear():
    updated = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    assert _recently_updated({"updated_at": updated}) is True


def test_old_pr_can_proceed_without_ci():
    updated = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert _recently_updated({"updated_at": updated}) is False
