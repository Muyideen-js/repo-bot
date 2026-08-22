import pytest

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
