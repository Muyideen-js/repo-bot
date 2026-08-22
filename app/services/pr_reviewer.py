"""Safe PR review pipeline for one immutable commit."""
import logging
import os
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import PRLog
from app.services import github as gh
from app.services.ai_review import AIRateLimitError, AIReviewError, review_pr

logger = logging.getLogger(__name__)

SPAM_COMMENTS = [
    "The automated review encountered a parsing issue. A maintainer will review this PR manually.",
    "Automated review encountered an error",
]


async def review_current_pr(
    token: str,
    telegram_id: str,
    repo_full_name: str,
    pr_number: int,
    expected_sha: str,
    db: AsyncSession,
    silent: bool = False,
) -> str:
    """Review and act on one PR only when its head SHA remains unchanged."""
    pr = await gh.get_pr(token, repo_full_name, pr_number)
    if pr.get("state") != "open" or pr.get("draft"):
        return "CLOSED" if pr.get("state") != "open" else "DRAFT"
    current_sha = pr["head"]["sha"]
    if current_sha != expected_sha:
        return "STALE"

    pr_title = pr.get("title", "")
    pr_body = pr.get("body") or ""
    contributor = pr["user"]["login"]
    pr_url = pr.get("html_url", "")
    issue_number = gh.extract_issue_number(pr_body)
    if issue_number is None:
        await gh.post_issue_comment(
            token, repo_full_name, pr_number,
            f"Hey @{contributor},\n\nThis PR cannot be reviewed automatically because it "
            "does not reference the issue it solves. Add `Closes #<issue_number>`, "
            "`Fixes #<issue_number>`, or `Resolves #<issue_number>` to the description.",
        )
        await _log(db, repo_full_name, pr_number, pr_title, contributor, "SKIPPED", "No linked issue")
        return "SKIPPED"

    issue = await gh.get_issue(token, repo_full_name, issue_number)
    if "pull_request" in issue:
        await gh.post_issue_comment(
            token, repo_full_name, pr_number,
            f"@{contributor} — #{issue_number} is a pull request, not an issue. "
            "Please link the issue this change solves.",
        )
        return "INVALID_ISSUE"

    ci_status = await gh.get_ci_status(token, repo_full_name, current_sha)
    if ci_status == "pending" or (ci_status == "none" and _recently_updated(pr)):
        return "WAITING_CI"

    diff = await gh.get_pr_diff(token, repo_full_name, pr_number)
    try:
        review = await review_pr(
            issue_title=issue.get("title", ""),
            issue_body=issue.get("body") or "",
            pr_title=pr_title,
            pr_body=pr_body,
            diff=diff,
            ci_status=ci_status,
            contributor=contributor,
        )
    except AIRateLimitError:
        return "RATE_LIMITED"
    except AIReviewError as exc:
        logger.error("AI review failed for %s#%s: %s", repo_full_name, pr_number, exc)
        return "RETRY"

    approved = review["approved"] and ci_status != "failure"
    comment = _strip_emojis(review["comment"])
    summary = review["summary"]
    refreshed = await gh.get_pr(token, repo_full_name, pr_number)
    if refreshed.get("state") != "open" or refreshed["head"]["sha"] != expected_sha:
        return "STALE"

    if approved:
        try:
            await gh.post_review_comment(token, repo_full_name, pr_number, body=comment, approve=True)
        except httpx.HTTPStatusError as exc:
            logger.warning("Could not submit approval for %s#%s: %s", repo_full_name, pr_number, exc)
            await gh.post_issue_comment(token, repo_full_name, pr_number, comment)
        merged = await gh.merge_pr(
            token, repo_full_name, pr_number, pr_title, expected_sha=expected_sha
        )
        decision = "MERGED" if merged else "MERGE_BLOCKED"
    else:
        await gh.post_review_comment(token, repo_full_name, pr_number, body=comment, approve=False)
        decision = "CHANGES_REQUESTED"

    await _log(db, repo_full_name, pr_number, pr_title, contributor, decision, summary)
    if not silent:
        await notify_telegram(
            telegram_id,
            f"PR #{pr_number} — {decision.replace('_', ' ')} in {repo_full_name}\n{summary}\n{pr_url}",
        )
    return decision


def _recently_updated(pr: dict, grace_seconds: int = 120) -> bool:
    updated_at = pr.get("updated_at")
    if not updated_at:
        return True
    updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - updated).total_seconds() < grace_seconds


async def cleanup_spam_comments(token: str, telegram_id: str, repo_full_name: str) -> int:
    return await gh.delete_bot_comments(token, repo_full_name, SPAM_COMMENTS)


def _strip_emojis(text: str) -> str:
    emoji_pattern = re.compile(
        "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E0-\U0001F1FF\u200d\ufe0f]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub("", text).strip()


async def _log(db, repo, pr_number, pr_title, contributor, decision, reason):
    db.add(PRLog(
        repo_full_name=repo,
        pr_number=pr_number,
        pr_title=pr_title,
        contributor=contributor,
        decision=decision,
        reason=reason,
    ))
    await db.commit()


async def notify_telegram(telegram_id: str, message: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": telegram_id, "text": message},
            )
            response.raise_for_status()
    except Exception as exc:
        logger.error("Failed to send Telegram notification: %s", exc)
