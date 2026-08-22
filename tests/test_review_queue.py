import pytest

from app.services import review_queue


@pytest.mark.asyncio
async def test_scan_reports_discovered_and_actually_scheduled(monkeypatch):
    pull_requests = [
        {"number": 1, "head": {"sha": "new"}},
        {"number": 2, "head": {"sha": "done"}},
    ]

    async def fake_get_all(token, repo):
        return pull_requests

    async def fake_enqueue(repo, pr):
        return pr["number"] == 1

    monkeypatch.setattr(review_queue.gh, "get_all_open_prs", fake_get_all)
    monkeypatch.setattr(review_queue, "enqueue_pr", fake_enqueue)

    assert await review_queue.enqueue_all_open_prs("token", "owner/repo") == (2, 1)


@pytest.mark.parametrize(
    "status,last_error,force,expected",
    [
        ("DONE", "MERGE_BLOCKED", False, True),
        ("DONE", "MERGED", False, False),
        ("FAILED", "error", False, True),
        ("DONE", "MERGED", True, True),
    ],
)
def test_only_retryable_existing_jobs_are_rescheduled(
    status, last_error, force, expected
):
    job = type("Job", (), {"status": status, "last_error": last_error})()
    assert review_queue._should_reschedule_existing_job(job, force) is expected
