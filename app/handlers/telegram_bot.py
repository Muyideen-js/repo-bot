"""Telegram onboarding and control interface."""
import logging
import os

import httpx
from sqlalchemy import func, select
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from app.models.database import AsyncSessionLocal, Repo, ReviewJob, User
from app.services import github as gh
from app.services.crypto import decrypt_token, encrypt_token
from app.services.review_queue import enqueue_all_open_prs

logger = logging.getLogger(__name__)
WAITING_FOR_TOKEN = 1
WAITING_FOR_REPO = 2
WAITING_FOR_REMOVE_REPO = 3
WAITING_FOR_SCAN_CHOICE = 4
WAITING_FOR_CLEAN_CHOICE = 5


async def _get_user_and_token(telegram_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
    if not user or not user.setup_complete or not user.github_token_encrypted:
        return None, None
    return user, decrypt_token(user.github_token_encrypted)


async def _fetch_github_repos(token: str) -> list[str]:
    repos = []
    page = 1
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            response = await client.get(
                "https://api.github.com/user/repos",
                headers=gh._headers(token),
                params={
                    "per_page": 100,
                    "page": page,
                    "affiliation": "owner,collaborator,organization_member",
                    "sort": "full_name",
                },
            )
            response.raise_for_status()
            batch = response.json()
            repos.extend(
                repo["full_name"] for repo in batch
                if (repo.get("permissions") or {}).get("push")
                or (repo.get("permissions") or {}).get("admin")
            )
            if len(batch) < 100:
                return repos
            page += 1


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))


async def _active_repos(telegram_id: str) -> list[Repo]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Repo).where(
            Repo.telegram_id == telegram_id,
            Repo.active.is_(True),
        ).order_by(Repo.full_name))
        return list(result.scalars())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to the GitHub PR Review Bot.\n\n"
        "I compare each PR with its linked issue, wait for CI, request precise changes "
        "when needed, and merge the exact reviewed commit when it is complete.\n\n"
        "Run /setup, then /addrepo. Existing open PRs are scanned when a repo is added."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/setup — connect GitHub\n"
        "/addrepo — monitor a repository and scan existing PRs\n"
        "/scanrepo — rescan all open PRs\n"
        "/listrepos — show monitored repositories\n"
        "/removerepo — stop monitoring a repository\n"
        "/cleanrepo — remove old bot error comments\n"
        "/status — show configuration and queued work\n"
        "/cancel — cancel the current command"
    )


async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a GitHub personal access token with repository contents, pull-request, "
        "issues, checks/status, webhook administration, and merge access. "
        "The message will be deleted immediately. Use /cancel to stop."
    )
    return WAITING_FOR_TOKEN


async def receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    telegram_id = str(update.effective_user.id)
    try:
        await update.message.delete()
    except Exception:
        pass
    username = await gh.validate_token(token)
    if not username:
        await update.effective_chat.send_message("That GitHub token is invalid. Run /setup to try again.")
        return ConversationHandler.END
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(telegram_id=telegram_id)
            db.add(user)
        user.github_token_encrypted = encrypt_token(token)
        user.github_username = username
        user.setup_complete = True
        await db.commit()
    await update.effective_chat.send_message(f"Connected as @{username}. Run /addrepo next.")
    return ConversationHandler.END


async def addrepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    user, token = await _get_user_and_token(telegram_id)
    if not user:
        await update.message.reply_text("Run /setup first.")
        return ConversationHandler.END
    await update.message.reply_text("Fetching repositories with push access...")
    try:
        github_repos = await _fetch_github_repos(token)
    except Exception as exc:
        logger.error("Could not fetch repositories: %s", exc)
        await update.message.reply_text("GitHub could not return your repositories. Check the token permissions.")
        return ConversationHandler.END
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Repo).where(Repo.active.is_(True)))
        monitored = {repo.full_name for repo in result.scalars()}
    available = [repo for repo in github_repos if repo not in monitored]
    if not available:
        await update.message.reply_text("No unmonitored repositories with push access were found.")
        return ConversationHandler.END
    context.user_data["addrepo_list"] = available
    await update.message.reply_text(
        "Choose a repository:\n\n" + _numbered(available) + "\n\nReply with its number."
    )
    return WAITING_FOR_REPO


async def receive_repo_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    choices = context.user_data.get("addrepo_list", [])
    text = update.message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= len(choices):
        await update.message.reply_text(f"Reply with a number from 1 to {len(choices)}.")
        return WAITING_FOR_REPO
    repo_name = choices[int(text) - 1]
    user, token = await _get_user_and_token(telegram_id)
    if not user:
        return ConversationHandler.END
    webhook_url = os.environ["PUBLIC_URL"].rstrip("/") + "/webhook/github"
    try:
        webhook_id = await gh.register_webhook(
            token, repo_name, webhook_url, os.environ["GITHUB_WEBHOOK_SECRET"]
        )
    except Exception as exc:
        logger.error("Webhook registration failed for %s: %s", repo_name, exc)
        await update.message.reply_text("Webhook registration failed. Check webhook-admin permission.")
        return ConversationHandler.END
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Repo).where(
            Repo.telegram_id == telegram_id,
            Repo.full_name == repo_name,
        ))
        repo = result.scalars().first()
        if not repo:
            repo = Repo(telegram_id=telegram_id, full_name=repo_name)
            db.add(repo)
        repo.webhook_id = webhook_id
        repo.active = True
        await db.commit()
    try:
        discovered, scheduled = await enqueue_all_open_prs(token, repo_name)
        scan_message = (
            f"Found {discovered} existing open PR(s); scheduled {scheduled} "
            "new review(s)."
        )
    except Exception as exc:
        logger.error("Initial scan failed for %s: %s", repo_name, exc)
        scan_message = "The initial scan failed; run /scanrepo to retry it."
    await update.message.reply_text(f"Now monitoring {repo_name}. {scan_message}")
    return ConversationHandler.END


async def list_repos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repos = await _active_repos(str(update.effective_user.id))
    await update.message.reply_text(
        "Monitored repositories:\n" + "\n".join(repo.full_name for repo in repos)
        if repos else "No repositories are being monitored."
    )


async def _choose_repo(update: Update, context, key: str, state: int, prompt: str):
    repos = await _active_repos(str(update.effective_user.id))
    if not repos:
        await update.message.reply_text("No repositories are being monitored.")
        return ConversationHandler.END
    context.user_data[key] = [repo.full_name for repo in repos]
    await update.message.reply_text(prompt + "\n\n" + _numbered(context.user_data[key]))
    return state


async def removerepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _choose_repo(update, context, "remove_repos", WAITING_FOR_REMOVE_REPO, "Choose a repository to remove:")


async def receive_remove_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    choices = context.user_data.get("remove_repos", [])
    text = update.message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= len(choices):
        await update.message.reply_text(f"Reply with a number from 1 to {len(choices)}.")
        return WAITING_FOR_REMOVE_REPO
    repo_name = choices[int(text) - 1]
    user, token = await _get_user_and_token(telegram_id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Repo).where(
            Repo.telegram_id == telegram_id, Repo.full_name == repo_name, Repo.active.is_(True)
        ))
        repo = result.scalars().first()
        if repo and repo.webhook_id and token:
            try:
                await gh.delete_webhook(token, repo_name, repo.webhook_id)
            except Exception as exc:
                logger.warning("Could not delete webhook for %s: %s", repo_name, exc)
        if repo:
            repo.active = False
            await db.commit()
    await update.message.reply_text(f"Stopped monitoring {repo_name}.")
    return ConversationHandler.END


async def scanrepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _choose_repo(update, context, "scan_repos", WAITING_FOR_SCAN_CHOICE, "Choose a repository to scan:")


async def receive_scan_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choices = context.user_data.get("scan_repos", [])
    text = update.message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= len(choices):
        await update.message.reply_text(f"Reply with a number from 1 to {len(choices)}.")
        return WAITING_FOR_SCAN_CHOICE
    repo_name = choices[int(text) - 1]
    user, token = await _get_user_and_token(str(update.effective_user.id))
    if not user:
        return ConversationHandler.END
    await update.message.reply_text("Queuing all existing open PRs...")
    discovered, scheduled = await enqueue_all_open_prs(token, repo_name)
    await update.message.reply_text(
        f"Found {discovered} open PR(s); scheduled {scheduled} new, failed, or "
        "merge-blocked review(s). "
        "Already completed commits were not duplicated."
    )
    return ConversationHandler.END


async def cleanrepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _choose_repo(update, context, "clean_repos", WAITING_FOR_CLEAN_CHOICE, "Choose a repository to clean:")


async def receive_clean_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choices = context.user_data.get("clean_repos", [])
    text = update.message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= len(choices):
        await update.message.reply_text(f"Reply with a number from 1 to {len(choices)}.")
        return WAITING_FOR_CLEAN_CHOICE
    repo_name = choices[int(text) - 1]
    user, token = await _get_user_and_token(str(update.effective_user.id))
    if not user:
        return ConversationHandler.END
    from app.services.pr_reviewer import cleanup_spam_comments
    count = await cleanup_spam_comments(token, user.telegram_id, repo_name)
    await update.message.reply_text(f"Deleted {count} old bot error comment(s).")
    return ConversationHandler.END


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    user, _ = await _get_user_and_token(telegram_id)
    repos = await _active_repos(telegram_id)
    counts = {"QUEUED": 0, "PROCESSING": 0, "DONE": 0, "FAILED": 0}
    wait_counts = {"RATE_LIMITED": 0, "WAITING_CI": 0}
    async with AsyncSessionLocal() as db:
        if repos:
            result = await db.execute(
                select(ReviewJob.status, func.count())
                .where(ReviewJob.repo_full_name.in_([repo.full_name for repo in repos]))
                .group_by(ReviewJob.status)
            )
            counts.update(dict(result.all()))
            wait_result = await db.execute(
                select(ReviewJob.last_error, func.count())
                .where(
                    ReviewJob.repo_full_name.in_([repo.full_name for repo in repos]),
                    ReviewJob.status == "QUEUED",
                    ReviewJob.last_error.in_(["RATE_LIMITED", "WAITING_CI"]),
                )
                .group_by(ReviewJob.last_error)
            )
            wait_counts.update(dict(wait_result.all()))
    if not user:
        await update.message.reply_text("Not configured. Run /setup.")
    else:
        await update.message.reply_text(
            f"GitHub: @{user.github_username}\n"
            f"Monitored repositories: {len(repos)}\n"
            f"Queued: {counts['QUEUED']}\n"
            f"  Waiting for AI quota: {wait_counts['RATE_LIMITED']}\n"
            f"  Waiting for CI: {wait_counts['WAITING_CI']}\n"
            f"Processing: {counts['PROCESSING']}\n"
            f"Completed: {counts['DONE']}\n"
            f"Failed: {counts['FAILED']}"
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Telegram handler failed", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        await update.effective_chat.send_message("That operation failed. Please try again shortly.")


def build_telegram_app() -> Application:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    conversations = [
        ConversationHandler([CommandHandler("setup", setup_start)], {WAITING_FOR_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token)]}, [CommandHandler("cancel", cancel)]),
        ConversationHandler([CommandHandler("addrepo", addrepo_start)], {WAITING_FOR_REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_repo_choice)]}, [CommandHandler("cancel", cancel)]),
        ConversationHandler([CommandHandler("removerepo", removerepo_start)], {WAITING_FOR_REMOVE_REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_remove_choice)]}, [CommandHandler("cancel", cancel)]),
        ConversationHandler([CommandHandler("scanrepo", scanrepo_start)], {WAITING_FOR_SCAN_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_scan_choice)]}, [CommandHandler("cancel", cancel)]),
        ConversationHandler([CommandHandler("cleanrepo", cleanrepo_start)], {WAITING_FOR_CLEAN_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_clean_choice)]}, [CommandHandler("cancel", cancel)]),
    ]
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("listrepos", list_repos))
    app.add_handler(CommandHandler("status", status))
    for conversation in conversations:
        app.add_handler(conversation)
    app.add_error_handler(error_handler)
    return app
