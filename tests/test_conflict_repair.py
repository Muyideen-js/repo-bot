from pathlib import Path

import pytest

from app.services.conflict_ai import ConflictResolutionError, _validate_resolutions
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
