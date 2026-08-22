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
