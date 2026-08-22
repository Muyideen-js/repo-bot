"""Durable review queue and background worker."""
import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.database import AsyncSessionLocal, Repo, ReviewJob, User
from app.services import github as gh
from app.services.crypto import decrypt_token
from app.services.pr_reviewer import notify_telegram, review_current_pr

logger = logging.getLogger(__name__)
_quota_notified_until: dict[str, datetime] = {}
TERMINAL_RESULTS = {
    "MERGED", "MERGE_BLOCKED", "CHANGES_REQUESTED", "SKIPPED",
    "INVALID_ISSUE", "CLOSED",
}


async def enqueue_pr(repo_full_name: str, pr: dict, force: bool = False) -> bool:
    """Idempotently queue the current commit of an open PR."""
    number = pr.get("number")
    sha = (pr.get("head") or {}).get("sha")
    if not number or not sha:
        return False
    async with AsyncSessionLocal() as db:
        repo_result = await db.execute(
            select(Repo).where(Repo.full_name == repo_full_name, Repo.active.is_(True))
        )
        if not repo_result.scalars().first():
            return False
        result = await db.execute(select(ReviewJob).where(
            ReviewJob.repo_full_name == repo_full_name,
            ReviewJob.pr_number == number,
            ReviewJob.head_sha == sha,
        ))
        job = result.scalar_one_or_none()
        if job:
            should_schedule = force or job.status == "FAILED"
            if should_schedule:
                job.status = "QUEUED"
                job.attempts = 0
                job.next_attempt_at = datetime.utcnow()
                job.last_error = None
                await db.commit()
            elif job.status == "QUEUED" and job.last_error == "WAITING_CI":
                # CI events may wake CI-waiting work, but must not bypass an
                # AI quota or transient-error cooldown.
                job.next_attempt_at = datetime.utcnow()
                await db.commit()
            return should_schedule
        db.add(ReviewJob(repo_full_name=repo_full_name, pr_number=number, head_sha=sha))
        try:
            await db.commit()
            return True
        except IntegrityError:
            await db.rollback()
            return False


async def enqueue_all_open_prs(token: str, repo_full_name: str) -> tuple[int, int]:
    """Return (open PRs discovered, commits newly scheduled or retried)."""
    pull_requests = await gh.get_all_open_prs(token, repo_full_name)
    scheduled = 0
    for pr in pull_requests:
        scheduled += int(await enqueue_pr(repo_full_name, pr))
    return len(pull_requests), scheduled


async def wake_jobs_for_sha(repo_full_name: str, sha: str) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ReviewJob).where(
            ReviewJob.repo_full_name == repo_full_name,
            ReviewJob.head_sha == sha,
            ReviewJob.status == "QUEUED",
            ReviewJob.last_error == "WAITING_CI",
        ))
        for job in result.scalars():
            job.next_attempt_at = datetime.utcnow()
        await db.commit()


async def review_worker(stop_event: asyncio.Event) -> None:
    """Process queued reviews one at a time, retrying recoverable states."""
    await _recover_interrupted_jobs()
    while not stop_event.is_set():
        job_id = await _claim_next_job()
        if job_id is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            continue
        await _process_job(job_id)


async def _claim_next_job() -> int | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ReviewJob)
            .where(
                ReviewJob.status == "QUEUED",
                ReviewJob.next_attempt_at <= datetime.utcnow(),
            )
            .order_by(ReviewJob.next_attempt_at, ReviewJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = result.scalar_one_or_none()
        if not job:
            return None
        job.status = "PROCESSING"
        await db.commit()
        return job.id


async def _process_job(job_id: int) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(ReviewJob, job_id)
        if not job:
            return
        logger.info(
            "Review job started id=%s repo=%s pr=%s sha=%s attempt=%s",
            job.id, job.repo_full_name, job.pr_number, job.head_sha[:12], job.attempts + 1,
        )
        repo_result = await db.execute(select(Repo).where(
            Repo.full_name == job.repo_full_name,
            Repo.active.is_(True),
        ))
        repo = repo_result.scalars().first()
        if not repo:
            await _finish(job, db, "DONE", "Repository is no longer monitored")
            return
        user_result = await db.execute(select(User).where(User.telegram_id == repo.telegram_id))
        user = user_result.scalar_one_or_none()
        if not user or not user.github_token_encrypted:
            await _finish(job, db, "FAILED", "Repository owner is not configured")
            return

        token = decrypt_token(user.github_token_encrypted)
        try:
            outcome = await review_current_pr(
                token, user.telegram_id, job.repo_full_name, job.pr_number,
                job.head_sha, db,
            )
            if outcome == "WAITING_CI" or outcome == "DRAFT":
                await _reschedule(job, db, 60 if outcome == "WAITING_CI" else 300, outcome)
            elif outcome == "RATE_LIMITED":
                await _reschedule(job, db, 3600, outcome)
                await _notify_quota_once(user.telegram_id, job.repo_full_name)
            elif outcome == "RETRY":
                await _retry_or_fail(job, db, "AI review failed")
            elif outcome == "STALE":
                current = await gh.get_pr(token, job.repo_full_name, job.pr_number)
                await _finish(job, db, "DONE", "Superseded by a newer commit")
                await enqueue_pr(job.repo_full_name, current)
            else:
                await _finish(job, db, "DONE", outcome)
            logger.info(
                "Review job finished id=%s repo=%s pr=%s outcome=%s status=%s",
                job.id, job.repo_full_name, job.pr_number, outcome, job.status,
            )
        except Exception as exc:
            logger.exception("Review job %s failed", job.id)
            await _retry_or_fail(job, db, str(exc))
            if job.status == "FAILED":
                await notify_telegram(
                    user.telegram_id,
                    f"Review failed for {job.repo_full_name} PR #{job.pr_number} after retries. "
                    "A maintainer should inspect it manually.",
                )


async def _retry_or_fail(job: ReviewJob, db, error: str) -> None:
    job.attempts += 1
    if job.attempts >= 5:
        await _finish(job, db, "FAILED", error[:1000])
    else:
        await _reschedule(job, db, min(900, 30 * (2 ** (job.attempts - 1))), error)


async def _reschedule(job: ReviewJob, db, seconds: int, reason: str) -> None:
    job.status = "QUEUED"
    job.next_attempt_at = datetime.utcnow() + timedelta(seconds=seconds)
    job.last_error = reason[:1000]
    await db.commit()


async def _finish(job: ReviewJob, db, status: str, detail: str) -> None:
    job.status = status
    job.last_error = detail[:1000]
    await db.commit()


async def _recover_interrupted_jobs() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ReviewJob).where(ReviewJob.status == "PROCESSING"))
        for job in result.scalars():
            job.status = "QUEUED"
            job.next_attempt_at = datetime.utcnow()
        await db.commit()


async def _notify_quota_once(telegram_id: str, repo_full_name: str) -> None:
    now = datetime.utcnow()
    if _quota_notified_until.get(telegram_id, datetime.min) > now:
        return
    _quota_notified_until[telegram_id] = now + timedelta(hours=1)
    await notify_telegram(
        telegram_id,
        f"AI quota limit reached while reviewing {repo_full_name}. Remaining PRs "
        "are still queued and will retry after the cooldown. Use /status for progress.",
    )


async def sync_registered_webhooks() -> None:
    """Upgrade hooks created by older versions to include CI events."""
    import os
    webhook_url = os.environ["PUBLIC_URL"].rstrip("/") + "/webhook/github"
    secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Repo).where(Repo.active.is_(True)))
        repos = result.scalars().all()
        for repo in repos:
            user_result = await db.execute(select(User).where(User.telegram_id == repo.telegram_id))
            user = user_result.scalar_one_or_none()
            if not user or not user.github_token_encrypted or not repo.webhook_id:
                continue
            try:
                await gh.update_webhook(
                    decrypt_token(user.github_token_encrypted), repo.full_name,
                    repo.webhook_id, webhook_url, secret,
                )
            except Exception as exc:
                logger.error("Could not update webhook for %s: %s", repo.full_name, exc)
