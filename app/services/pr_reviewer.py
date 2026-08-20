"""
Orchestrates the full PR review pipeline:
1. Check for Closes #issue in PR body
2. Fetch issue + diff + CI
3. Ask Gemini to review
4. Post comment and merge (or request changes)
5. Notify the repo owner on Telegram
"""
import os
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import Repo, PRLog, User
from app.services import github as gh
from app.services.ai_review import review_pr, AIRateLimitError, AIReviewError
from app.services.crypto import decrypt_token

logger = logging.getLogger(__name__)


async def handle_pr_event(payload: dict, db: AsyncSession) -> None:
    """Entry point called by the webhook handler for every PR event."""
    action = payload.get("action")
    if action not in ("opened", "reopened", "synchronize"):
        return

    pr = payload["pull_request"]
    repo_full_name = payload["repository"]["full_name"]
    pr_number = pr["number"]
    pr_title = pr.get("title", "")
    pr_body = pr.get("body") or ""
    contributor = pr["user"]["login"]
    head_sha = pr["head"]["sha"]
    pr_url = pr.get("html_url", "")

    result = await db.execute(
        select(Repo).where(Repo.full_name == repo_full_name, Repo.active == True)
    )
    repo_config = result.scalar_one_or_none()
    if not repo_config:
        return

    user_result = await db.execute(
        select(User).where(User.telegram_id == repo_config.telegram_id)
    )
    user = user_result.scalar_one_or_none()
    if not user or not user.github_token_encrypted:
        return

    token = decrypt_token(user.github_token_encrypted)
    telegram_id = user.telegram_id

    await _review_single_pr(
        token=token,
        telegram_id=telegram_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_body=pr_body,
        contributor=contributor,
        head_sha=head_sha,
        pr_url=pr_url,
        db=db,
    )


async def scan_all_open_prs(
    token: str,
    telegram_id: str,
    repo_full_name: str,
    db: AsyncSession,
) -> list[dict]:
    """
    Fetch all open PRs on a repo and review each one.
    Returns a list of result dicts for Telegram reporting.
    """
    open_prs = await gh.get_open_prs(token, repo_full_name)
    if not open_prs:
        return []

    results = []
    for pr in open_prs:
        pr_number = pr["number"]
        pr_title = pr.get("title", "")
        pr_body = pr.get("body") or ""
        contributor = pr["user"]["login"]
        head_sha = pr["head"]["sha"]
        pr_url = pr.get("html_url", "")

        outcome = await _review_single_pr(
            token=token,
            telegram_id=telegram_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_body=pr_body,
            contributor=contributor,
            head_sha=head_sha,
            pr_url=pr_url,
            db=db,
            silent=True,  # don't send individual Telegram messages during scan
        )
        results.append({
            "pr_number": pr_number,
            "pr_title": pr_title,
            "pr_url": pr_url,
            "outcome": outcome,
        })

    return results


async def _review_single_pr(
    token: str,
    telegram_id: str,
    repo_full_name: str,
    pr_number: int,
    pr_title: str,
    pr_body: str,
    contributor: str,
    head_sha: str,
    pr_url: str,
    db: AsyncSession,
    silent: bool = False,
) -> str:
    """
    Review one PR end-to-end. Returns outcome string:
    MERGED | COMMENTED | SKIPPED | RATE_LIMITED | ERROR
    """

    # ── Step 1: Check for Closes #N ───────────────────────────────────────────
    issue_number = gh.extract_issue_number(pr_body)
    if issue_number is None:
        comment = (
            f"👋 Hey @{contributor}!\n\n"
            "This PR cannot be reviewed or merged automatically because it doesn't "
            "reference the issue it solves.\n\n"
            "Please update your PR description to include one of:\n"
            "- `Closes #<issue_number>`\n"
            "- `Fixes #<issue_number>`\n"
            "- `Resolves #<issue_number>`\n\n"
            "This links your work to the issue and allows the automated review "
            "system to verify the fix. Thank you! 🙏"
        )
        await gh.post_issue_comment(token, repo_full_name, pr_number, comment)
        await _log(db, repo_full_name, pr_number, pr_title, contributor, "SKIPPED",
                   "No Closes #issue in PR body")
        if not silent:
            await _notify_telegram(
                telegram_id,
                f"⚠️ *PR #{pr_number}* in `{repo_full_name}` by @{contributor}\n"
                f"Skipped — no `Closes #issue` in description.\n{pr_url}",
            )
        return "SKIPPED"

    # ── Step 2: Fetch issue ────────────────────────────────────────────────────
    try:
        issue = await gh.get_issue(token, repo_full_name, issue_number)
    except Exception:
        await gh.post_issue_comment(
            token, repo_full_name, pr_number,
            f"⚠️ Could not fetch issue #{issue_number}. Please ensure the issue exists."
        )
        return "ERROR"

    issue_title = issue.get("title", "")
    issue_body = issue.get("body") or ""

    # ── Step 3: Fetch diff and CI ──────────────────────────────────────────────
    diff = await gh.get_pr_diff(token, repo_full_name, pr_number)
    ci_status = await gh.get_ci_status(token, repo_full_name, head_sha)

    if ci_status == "pending":
        await gh.post_issue_comment(
            token, repo_full_name, pr_number,
            f"⏳ @{contributor} — CI checks are still running. "
            "The bot will review once they complete."
        )
        return "SKIPPED"

    # ── Step 4: AI Review ──────────────────────────────────────────────────────
    try:
        review = await review_pr(
            issue_title=issue_title,
            issue_body=issue_body,
            pr_title=pr_title,
            pr_body=pr_body,
            diff=diff,
            ci_status=ci_status,
            contributor=contributor,
        )
    except AIRateLimitError:
        # Gemini quota hit — notify maintainer, comment on PR, do NOT merge
        rate_limit_comment = (
            f"⏳ @{contributor} — The automated review is temporarily unavailable "
            "because the AI service has hit its daily quota.\n\n"
            "Your PR will be reviewed automatically once the quota resets (usually within 1 hour). "
            "No action needed from you."
        )
        await gh.post_issue_comment(token, repo_full_name, pr_number, rate_limit_comment)
        await _log(db, repo_full_name, pr_number, pr_title, contributor, "RATE_LIMITED",
                   "Gemini daily quota exceeded")
        await _notify_telegram(
            telegram_id,
            f"⚠️ *AI Rate Limit Hit*\n\n"
            f"Could not review PR #{pr_number} in `{repo_full_name}`.\n"
            f"Gemini's daily quota is exhausted. The PR has been notified.\n"
            f"Review will resume automatically when quota resets.\n{pr_url}",
        )
        return "RATE_LIMITED"

    except AIReviewError as e:
        logger.error(f"AI review failed for PR #{pr_number}: {e}")
        await gh.post_issue_comment(
            token, repo_full_name, pr_number,
            f"⚠️ @{contributor} — Automated review encountered an error. "
            "A maintainer will review this PR manually."
        )
        return "ERROR"

    # ── Step 5: Act on the review ─────────────────────────────────────────────
    approved = review.get("approved", False)
    comment = review.get("comment", "Automated review complete.")
    summary = review.get("summary", "")

    if approved and ci_status != "failure":
        await gh.post_review_comment(token, repo_full_name, pr_number,
                                     body=comment, approve=True)
        merged = await gh.merge_pr(token, repo_full_name, pr_number, pr_title)
        decision = "MERGED" if merged else "COMMENTED"

        if not silent:
            await _notify_telegram(
                telegram_id,
                f"✅ *PR #{pr_number} MERGED* in `{repo_full_name}`\n"
                f"*Title:* {pr_title}\n"
                f"*Contributor:* @{contributor}\n"
                f"*Verdict:* {summary}\n{pr_url}",
            )
    else:
        await gh.post_review_comment(token, repo_full_name, pr_number,
                                     body=comment, approve=False)
        decision = "COMMENTED"

        if not silent:
            await _notify_telegram(
                telegram_id,
                f"🔍 *PR #{pr_number} NEEDS CHANGES* in `{repo_full_name}`\n"
                f"*Title:* {pr_title}\n"
                f"*Contributor:* @{contributor}\n"
                f"*Verdict:* {summary}\n{pr_url}",
            )

    await _log(db, repo_full_name, pr_number, pr_title, contributor, decision, summary)
    return decision


async def _log(db, repo, pr_number, pr_title, contributor, decision, reason):
    from app.models.database import PRLog
    log = PRLog(
        repo_full_name=repo,
        pr_number=pr_number,
        pr_title=pr_title,
        contributor=contributor,
        decision=decision,
        reason=reason,
    )
    db.add(log)
    await db.commit()


async def _notify_telegram(telegram_id: str, message: str) -> None:
    import httpx
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": telegram_id, "text": message, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")
