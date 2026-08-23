from app.handlers.webhook import PR_ACTIONS


def test_contributor_repush_is_a_review_trigger():
    assert "synchronize" in PR_ACTIONS
