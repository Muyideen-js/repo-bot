import asyncio

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


@pytest.mark.asyncio
async def test_repository_poller_scans_immediately_and_stops_cleanly(monkeypatch):
    stop_event = asyncio.Event()
    calls = 0

    async def fake_scan():
        nonlocal calls
        calls += 1
        stop_event.set()

    monkeypatch.setattr(review_queue, "_scan_monitored_repositories_once", fake_scan)
    await review_queue.repository_poller(stop_event)
    assert calls == 1


@pytest.mark.asyncio
async def test_repository_poller_survives_one_failed_cycle(monkeypatch):
    stop_event = asyncio.Event()
    calls = 0

    async def fake_scan():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database error")
        stop_event.set()

    async def immediate_timeout(awaitable, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        return None

    monkeypatch.setattr(review_queue, "_scan_monitored_repositories_once", fake_scan)
    monkeypatch.setattr(review_queue.asyncio, "wait_for", immediate_timeout)
    await review_queue.repository_poller(stop_event)
    assert calls == 2
