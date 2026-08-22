import hashlib
import hmac

import pytest

from app.services import github


class FakeResponse:
    def __init__(self, data, status_code=200, text=""):
        self._data = data
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeClient:
    def __init__(self, responses=None, **kwargs):
        self.responses = list(responses or [])
        self.last_json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        return self.responses.pop(0)

    async def put(self, *args, **kwargs):
        self.last_json = kwargs["json"]
        return FakeResponse({}, 200)


@pytest.mark.asyncio
async def test_in_progress_check_is_pending(monkeypatch):
    client = FakeClient([
        FakeResponse({"check_runs": [{"status": "in_progress", "conclusion": None}]}),
        FakeResponse({"state": "pending", "total_count": 0, "statuses": []}),
    ])
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client)
    assert await github.get_ci_status("token", "owner/repo", "sha") == "pending"


@pytest.mark.asyncio
async def test_no_checks_or_statuses_is_none(monkeypatch):
    client = FakeClient([
        FakeResponse({"check_runs": []}),
        FakeResponse({"state": "pending", "total_count": 0, "statuses": []}),
    ])
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client)
    assert await github.get_ci_status("token", "owner/repo", "sha") == "none"


@pytest.mark.asyncio
async def test_merge_is_pinned_to_reviewed_sha(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client)
    assert await github.merge_pr("token", "owner/repo", 7, "Title", "reviewed-sha")
    assert client.last_json["sha"] == "reviewed-sha"


@pytest.mark.asyncio
async def test_diff_406_falls_back_to_files_api(monkeypatch):
    client = FakeClient([
        FakeResponse({}, 406),
        FakeResponse([{
            "filename": "contract/src/lib.rs",
            "status": "modified",
            "additions": 2,
            "deletions": 1,
            "patch": "@@ -1 +1 @@\n-old\n+new",
        }]),
    ])
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client)
    diff = await github.get_pr_diff("token", "owner/repo", 147)
    assert "contract/src/lib.rs" in diff
    assert "+new" in diff


def test_webhook_verification_fails_closed(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    assert github.verify_webhook_signature(b"{}", "") is False


def test_webhook_signature(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    body = b'{"ok":true}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert github.verify_webhook_signature(body, f"sha256={digest}") is True


@pytest.mark.parametrize("body, expected", [
    ("Closes #12", 12),
    ("fixes #99", 99),
    ("Resolves #7", 7),
    ("related to #4", None),
])
def test_extract_issue_number(body, expected):
    assert github.extract_issue_number(body) == expected
