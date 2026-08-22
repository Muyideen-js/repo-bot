import json

import pytest

from app.services import ai_review
from app.services.ai_review import AIReviewError, _validate_result


def test_valid_ai_result():
    result = {
        "approved": False,
        "summary": "Tests are missing.",
        "missing": ["Add tests"],
        "comment": "@author Please add tests.",
    }
    assert _validate_result(result) == result


@pytest.mark.parametrize("approved", ["false", 0, None])
def test_approved_must_be_a_real_boolean(approved):
    with pytest.raises(AIReviewError):
        _validate_result({
            "approved": approved,
            "summary": "No",
            "missing": [],
            "comment": "No",
        })


class FakeGeminiResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        result = {
            "approved": False,
            "summary": "A requirement is missing.",
            "missing": ["Add a test"],
            "comment": "@author Please add a test.",
        }
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps(result)}]},
            }]
        }


class FakeGeminiClient:
    def __init__(self):
        self.url = None
        self.headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        self.url = url
        self.headers = headers
        return FakeGeminiResponse()


@pytest.mark.asyncio
async def test_gemini_key_is_sent_in_header_not_url(monkeypatch):
    client = FakeGeminiClient()
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key")
    monkeypatch.setattr(ai_review.httpx, "AsyncClient", lambda **kwargs: client)
    result = await ai_review.review_pr("Issue", "Body", "PR", "Body", "diff", "success", "author")
    assert result["approved"] is False
    assert "secret-key" not in client.url
    assert client.headers["x-goog-api-key"] == "secret-key"
