from pathlib import Path

import pytest

from app.services.conflict_ai import ConflictResolutionError, _validate_resolutions
from app.services import conflict_ai
from app.services.ai_review import AIReviewError
from app.services.conflict_repair import (
    ConflictRepairError,
    _apply_resolutions,
    _collect_conflicts,
)


def test_valid_conflict_resolutions_are_returned_in_index_order():
    result = {
        "resolutions": [
            {"index": 2, "replacement": "second"},
            {"index": 1, "replacement": "first"},
        ]
    }
    assert _validate_resolutions(result, 2) == ["first", "second"]


def test_conflict_resolution_requires_every_block():
    with pytest.raises(ConflictResolutionError):
        _validate_resolutions(
            {"resolutions": [{"index": 1, "replacement": "only one"}]}, 2
        )


def test_conflict_resolution_rejects_remaining_markers():
    with pytest.raises(ConflictResolutionError):
        _validate_resolutions(
            {"resolutions": [{"index": 1, "replacement": "<<<<<<< HEAD"}]}, 1
        )


def test_collect_and_apply_conflict_blocks(tmp_path: Path):
    source = tmp_path / "src" / "lib.rs"
    source.parent.mkdir()
    source.write_text(
        "before\n<<<<<<< HEAD\nfrom_pr();\n=======\nfrom_main();\n>>>>>>> base\nafter\n",
        encoding="utf-8",
    )

    blocks, matches = _collect_conflicts(tmp_path, ["src/lib.rs"])
    assert blocks[0]["ours"] == "from_pr();\n"
    assert blocks[0]["theirs"] == "from_main();\n"

    _apply_resolutions(matches, ["from_main();\nfrom_pr();"])
    assert source.read_text(encoding="utf-8") == (
        "before\nfrom_main();\nfrom_pr();\nafter\n"
    )


def test_workflow_conflicts_are_never_auto_resolved(tmp_path: Path):
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "<<<<<<< HEAD\none\n=======\ntwo\n>>>>>>> base\n", encoding="utf-8"
    )
    with pytest.raises(ConflictRepairError, match="sensitive file"):
        _collect_conflicts(tmp_path, [".github/workflows/ci.yml"])


@pytest.mark.asyncio
async def test_malformed_deepseek_output_is_retried_before_gemini(monkeypatch):
    calls = 0

    async def deepseek(prompt, expected_count):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AIReviewError("malformed JSON")
        return {"resolutions": [{"index": 1, "replacement": "resolved();"}]}

    async def unexpected_gemini(prompt):
        pytest.fail("Gemini must not run after a successful DeepSeek retry")

    async def no_sleep(*args):
        return None

    monkeypatch.setenv("AI_CONFLICT_PROVIDER_RETRIES", "2")
    monkeypatch.setattr(conflict_ai, "_deepseek_resolution", deepseek)
    monkeypatch.setattr(conflict_ai, "_gemini_resolution", unexpected_gemini)
    monkeypatch.setattr(conflict_ai.asyncio, "sleep", no_sleep)

    blocks = [{
        "index": 1,
        "path": "src/lib.rs",
        "before": "",
        "ours": "ours();",
        "theirs": "theirs();",
        "after": "",
    }]
    result = await conflict_ai.resolve_conflict_blocks(
        "Issue", "Body", "PR", "Body", blocks
    )
    assert result == ["resolved();"]
    assert calls == 2


class FakeStrictResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "submit_conflict_resolutions",
                            "arguments": '{"resolutions":[{"index":1,"replacement":"ok();"}]}',
                        }
                    }]
                },
            }]
        }


class FakeStrictClient:
    def __init__(self):
        self.url = None
        self.payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        self.url = url
        self.payload = json
        return FakeStrictResponse()


@pytest.mark.asyncio
async def test_deepseek_conflicts_use_strict_beta_tool_call(monkeypatch):
    client = FakeStrictClient()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setattr(conflict_ai.httpx, "AsyncClient", lambda **kwargs: client)

    result = await conflict_ai._deepseek_resolution("resolve this", 1)
    function = client.payload["tools"][0]["function"]
    assert client.url == "https://api.deepseek.com/beta/chat/completions"
    assert function["strict"] is True
    assert function["parameters"]["additionalProperties"] is False
    assert result["resolutions"][0]["replacement"] == "ok();"
