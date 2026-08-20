"""
Telegram bot interface.
Users set up the bot here: provide GitHub token, add repos, remove repos.

Commands:
  /start      — welcome + instructions
  /setup      — begin GitHub token setup
  /addrepo    — pick a repo from your GitHub list to monitor
  /listrepos  — show all registered repos
  /removerepo — pick a repo from your list to remove
  /scanrepo   — pick a repo to scan all existing open PRs
  /status     — show bot status for your account
  /help       — show all commands
"""
import os
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from sqlalchemy import select
from app.models.database import AsyncSessionLocal, User, Repo
from app.services.crypto import encrypt_token
from app.services import github as gh

logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_TOKEN       = 1
WAITING_FOR_REPO        = 2
WAITING_FOR_REMOVE_REPO = 3
WAITING_FOR_SCAN_CHOICE = 4
WAITING_FOR_CLEAN_CHOICE = 5


# ── helpers ───────────────────────────────────────────────────────────────────
async def _get_user_and_token(telegram_id: str):
    """Return (user, plain_token) or (None, None) if not set up."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
    if not user or not user.setup_complete:
        return None, None
    from app.services.crypto import decrypt_token
    return user, decrypt_token(user.github_token_encrypted)


async def _fetch_github_repos(token: str) -> list[str]:
    """
    Fetch all repos the token has push access to:
    own repos + org repos + collaborator repos.
    Returns list of 'owner/repo' strings.
    """
    import httpx
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    repos = []
    async with httpx.AsyncClient(timeout=15) as client:
        # Own + org repos
        page = 1
        while True:
            r = await client.get(
                "https://api.github.com/user/repos",
                headers=headers,
                params={"per_page": 100, "page": page, "affiliation": "owner,collaborator,organization_member"},
            )
            if r.status_code != 200:
                break
            data = r.json()
            if not data:
                break
            for repo in data:
                perms = repo.get("permissions", {})
                if perms.get("push") or perms.get("admin"):
                    repos.append(repo["full_name"])
            if len(data) < 100:
                break
            page += 1
    return repos


def _numbered_list(items: list[str]) -> str:
    return "\n".join(f"{i+1}. `{item}`" for i, item in enumerate(items))


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the *GitHub PR Review Bot*!\n\n"
        "I automatically review Pull Requests on your repos:\n"
        "• ✅ PR fully solves the issue → I merge it\n"
        "• ❌ PR is incomplete → I tag the contributor with exactly what to fix\n"
        "• ⚠️ PR has no `Closes #issue` → I block it and explain why\n\n"
        "To get started:\n"
        "1. Run /setup to connect your GitHub account\n"
        "2. Run /addrepo to pick a repo to monitor\n"
        "3. That's it — I'll handle the rest!\n\n"
        "Run /help to see all commands.",
        parse_mode="Markdown",
    )


# ── /help ─────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Available Commands:*\n\n"
        "/setup — Connect your GitHub account\n"
        "/addrepo — Pick a repo from your GitHub to monitor\n"
        "/scanrepo — Review existing open PRs (5 at a time)\n"
        "/cleanrepo — Delete spam comments the bot previously posted\n"
        "/listrepos — See your registered repos\n"
        "/removerepo — Remove a repo from monitoring\n"
        "/status — Check your account status\n"
        "/help — Show this message\n\n"
        "*How it works:*\n"
        "After setup, I register a webhook on each repo you add. "
        "When a PR is opened or updated, I review it automatically and notify you here.",
        parse_mode="Markdown",
    )


# ── /setup ────────────────────────────────────────────────────────────────────
async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔑 *GitHub Token Setup*\n\n"
        "I need a GitHub Personal Access Token (classic) with these permissions:\n"
        "• `repo` — full repo access\n"
        "• `write:repo_hook` — register webhooks\n\n"
        "👉 Create one at: https://github.com/settings/tokens/new\n\n"
        "Once you have it, paste it here.\n"
        "Your token is *encrypted* before storage — only you can use it.\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_TOKEN


async def receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    telegram_id = str(update.effective_user.id)

    # Delete the message immediately for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await update.message.reply_text("⏳ Validating your token with GitHub...")
    github_username = await gh.validate_token(token)
    if not github_username:
        await update.message.reply_text(
            "❌ That token doesn't seem to be valid.\n"
            "Make sure it's a *classic* token with `repo` + `write:repo_hook` permissions.\n"
            "Try /setup again.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    encrypted = encrypt_token(token)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user:
            user.github_token_encrypted = encrypted
            user.github_username = github_username
            user.setup_complete = True
        else:
            user = User(
                telegram_id=telegram_id,
                github_token_encrypted=encrypted,
                github_username=github_username,
                setup_complete=True,
            )
            db.add(user)
        await db.commit()

    await update.message.reply_text(
        f"✅ Connected as *{github_username}*!\n\n"
        "Now run /addrepo to pick a repo to monitor.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── /addrepo ──────────────────────────────────────────────────────────────────
async def addrepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    user, token = await _get_user_and_token(telegram_id)
    if not user:
        await update.message.reply_text("Please run /setup first to connect your GitHub account.")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Fetching your GitHub repos...")

    repos = await _fetch_github_repos(token)
    if not repos:
        await update.message.reply_text(
            "❌ Couldn't find any repos with push access on your account.\n"
            "Make sure your token has the `repo` permission."
        )
        return ConversationHandler.END

    # Filter out already-registered ones
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Repo).where(Repo.telegram_id == telegram_id, Repo.active == True)
        )
        already = {r.full_name for r in result.scalars().all()}

    available = [r for r in repos if r not in already]
    if not available:
        await update.message.reply_text(
            "All your repos are already being monitored!\n"
            "Run /listrepos to see them."
        )
        return ConversationHandler.END

    context.user_data["addrepo_list"] = available
    await update.message.reply_text(
        f"📦 *Pick a repo to monitor:*\n\n"
        + _numbered_list(available)
        + "\n\nReply with the number. Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_REPO


async def receive_repo_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    repos = context.user_data.get("addrepo_list", [])
    text = update.message.text.strip()

    if not text.isdigit() or int(text) < 1 or int(text) > len(repos):
        await update.message.reply_text(
            f"Please reply with a number between 1 and {len(repos)}."
        )
        return WAITING_FOR_REPO

    repo_name = repos[int(text) - 1]
    await update.message.reply_text(
        f"⏳ Registering `{repo_name}`...", parse_mode="Markdown"
    )

    user, token = await _get_user_and_token(telegram_id)

    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    webhook_url = f"{public_url}/webhook/github"
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "default_secret")

    try:
        webhook_id = await gh.register_webhook(token, repo_name, webhook_url, secret)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to register webhook: {str(e)}\n"
            "Make sure your token has `write:repo_hook` permission.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    async with AsyncSessionLocal() as db:
        repo = Repo(
            telegram_id=telegram_id,
            full_name=repo_name,
            webhook_id=webhook_id,
        )
        db.add(repo)
        await db.commit()

    await update.message.reply_text(
        f"✅ Now monitoring *{repo_name}*!\n\n"
        "When a PR is opened:\n"
        "• I check for `Closes #issue` in the description\n"
        "• Review the diff against the issue using AI\n"
        "• Merge if fully solved, or request changes if not\n"
        "• Notify you here either way 🔔",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── /listrepos ────────────────────────────────────────────────────────────────
async def list_repos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Repo).where(Repo.telegram_id == telegram_id, Repo.active == True)
        )
        repos = result.scalars().all()

    if not repos:
        await update.message.reply_text(
            "You have no repos registered yet. Run /addrepo to add one."
        )
        return

    lines = [f"• `{r.full_name}`" for r in repos]
    await update.message.reply_text(
        f"*Your monitored repos ({len(repos)}):*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


# ── /removerepo ───────────────────────────────────────────────────────────────
async def removerepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Repo).where(Repo.telegram_id == telegram_id, Repo.active == True)
        )
        repos = result.scalars().all()

    if not repos:
        await update.message.reply_text("You have no repos to remove.")
        return ConversationHandler.END

    context.user_data["remove_repos"] = [r.full_name for r in repos]
    await update.message.reply_text(
        "*Which repo do you want to remove?*\n\n"
        + _numbered_list([r.full_name for r in repos])
        + "\n\nReply with the number. Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_REMOVE_REPO


async def receive_remove_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    repos = context.user_data.get("remove_repos", [])
    text = update.message.text.strip()

    if not text.isdigit() or int(text) < 1 or int(text) > len(repos):
        await update.message.reply_text(
            f"Please reply with a number between 1 and {len(repos)}."
        )
        return WAITING_FOR_REMOVE_REPO

    repo_name = repos[int(text) - 1]

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Repo).where(
                Repo.telegram_id == telegram_id,
                Repo.full_name == repo_name,
                Repo.active == True,
            )
        )
        repo = result.scalar_one_or_none()
        if not repo:
            await update.message.reply_text("❌ Repo not found.")
            return ConversationHandler.END

        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        if user and repo.webhook_id:
            from app.services.crypto import decrypt_token
            token = decrypt_token(user.github_token_encrypted)
            try:
                await gh.delete_webhook(token, repo_name, repo.webhook_id)
            except Exception:
                pass

        repo.active = False
        await db.commit()

    await update.message.reply_text(
        f"✅ Removed `{repo_name}`. I'll no longer monitor PRs on that repo.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── /scanrepo ─────────────────────────────────────────────────────────────────
async def scanrepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Repo).where(Repo.telegram_id == telegram_id, Repo.active == True)
        )
        repos = result.scalars().all()

    if not repos:
        await update.message.reply_text(
            "You have no repos registered yet. Run /addrepo to add one."
        )
        return ConversationHandler.END

    context.user_data["scan_repos"] = [r.full_name for r in repos]
    await update.message.reply_text(
        "🔍 *Which repo do you want to scan?*\n\n"
        + _numbered_list([r.full_name for r in repos])
        + "\n\nReply with the number. Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_SCAN_CHOICE


async def receive_scan_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    repos = context.user_data.get("scan_repos", [])
    text = update.message.text.strip()

    if not text.isdigit() or int(text) < 1 or int(text) > len(repos):
        await update.message.reply_text(
            f"Please reply with a number between 1 and {len(repos)}."
        )
        return WAITING_FOR_SCAN_CHOICE

    repo_name = repos[int(text) - 1]
    page = context.user_data.get("scan_page", 1)

    await update.message.reply_text(
        f"Scanning {repo_name} — batch {page} (up to 5 PRs)..."
    )

    user, token = await _get_user_and_token(telegram_id)
    if not user:
        await update.message.reply_text("Please run /setup first.")
        return ConversationHandler.END

    async with AsyncSessionLocal() as db:
        from app.services.pr_reviewer import scan_all_open_prs
        results = await scan_all_open_prs(
            token=token,
            telegram_id=telegram_id,
            repo_full_name=repo_name,
            db=db,
            page=page,
            limit=5,
        )

    if not results:
        msg = "No open PRs found." if page == 1 else "No more PRs in this batch."
        await update.message.reply_text(msg)
        context.user_data.pop("scan_page", None)
        return ConversationHandler.END

    labels = {
        "MERGED":       "Merged",
        "COMMENTED":    "Needs changes — contributor tagged",
        "SKIPPED":      "Skipped — no Closes #issue",
        "RATE_LIMITED": "AI limit hit — PR notified",
        "ERROR":        "Error — manual review needed",
    }

    lines = []
    for r in results:
        label = labels.get(r["outcome"], r["outcome"])
        lines.append(f"PR #{r['pr_number']} — {r['pr_title'][:40]}\n   {label}")

    context.user_data["scan_page"] = page + 1

    rate_limited = any(r["outcome"] == "RATE_LIMITED" for r in results)
    footer = (
        "\n\nAI quota exhausted. Try /scanrepo again tomorrow to continue."
        if rate_limited
        else f"\n\nRun /scanrepo again to scan the next 5 PRs (batch {page + 1})."
    )

    await update.message.reply_text(
        f"Scan complete — {repo_name} (batch {page})\n"
        f"{len(results)} PR(s) reviewed\n\n"
        + "\n\n".join(lines)
        + footer,
    )
    return ConversationHandler.END


# ── /cleanrepo ────────────────────────────────────────────────────────────────
async def cleanrepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Repo).where(Repo.telegram_id == telegram_id, Repo.active == True)
        )
        repos = result.scalars().all()

    if not repos:
        await update.message.reply_text("You have no repos registered yet.")
        return ConversationHandler.END

    context.user_data["clean_repos"] = [r.full_name for r in repos]
    await update.message.reply_text(
        "Which repo do you want to clean spam comments from?\n\n"
        + _numbered_list([r.full_name for r in repos])
        + "\n\nReply with the number. Type /cancel to abort.",
    )
    return WAITING_FOR_CLEAN_CHOICE


async def receive_clean_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    repos = context.user_data.get("clean_repos", [])
    text = update.message.text.strip()

    if not text.isdigit() or int(text) < 1 or int(text) > len(repos):
        await update.message.reply_text(
            f"Please reply with a number between 1 and {len(repos)}."
        )
        return WAITING_FOR_CLEAN_CHOICE

    repo_name = repos[int(text) - 1]
    await update.message.reply_text(f"Cleaning spam comments on {repo_name}...")

    user, token = await _get_user_and_token(telegram_id)
    if not user:
        await update.message.reply_text("Please run /setup first.")
        return ConversationHandler.END

    from app.services.pr_reviewer import cleanup_spam_comments
    deleted = await cleanup_spam_comments(token, telegram_id, repo_name)

    await update.message.reply_text(
        f"Done. Deleted {deleted} spam comment(s) from {repo_name}."
    )
    return ConversationHandler.END


# ── /status ───────────────────────────────────────────────────────────────────
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        repo_result = await db.execute(
            select(Repo).where(Repo.telegram_id == telegram_id, Repo.active == True)
        )
        repos = repo_result.scalars().all()

    if not user or not user.setup_complete:
        await update.message.reply_text("❌ Not set up yet. Run /setup to connect your GitHub.")
        return

    await update.message.reply_text(
        f"*Your Bot Status*\n\n"
        f"GitHub: *@{user.github_username}* ✅\n"
        f"Monitored repos: *{len(repos)}*\n\n"
        + ("\n".join([f"• `{r.full_name}`" for r in repos]) if repos else "_No repos yet_"),
        parse_mode="Markdown",
    )


# ── /cancel ───────────────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. Run /help to see available commands.")
    return ConversationHandler.END


# ── app builder ───────────────────────────────────────────────────────────────
def build_telegram_app() -> Application:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    app = Application.builder().token(bot_token).build()

    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup", setup_start)],
        states={WAITING_FOR_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    addrepo_conv = ConversationHandler(
        entry_points=[CommandHandler("addrepo", addrepo_start)],
        states={WAITING_FOR_REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_repo_choice)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    removerepo_conv = ConversationHandler(
        entry_points=[CommandHandler("removerepo", removerepo_start)],
        states={WAITING_FOR_REMOVE_REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_remove_choice)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    scanrepo_conv = ConversationHandler(
        entry_points=[CommandHandler("scanrepo", scanrepo_start)],
        states={WAITING_FOR_SCAN_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_scan_choice)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    cleanrepo_conv = ConversationHandler(
        entry_points=[CommandHandler("cleanrepo", cleanrepo_start)],
        states={WAITING_FOR_CLEAN_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_clean_choice)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("listrepos", list_repos))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(setup_conv)
    app.add_handler(addrepo_conv)
    app.add_handler(removerepo_conv)
    app.add_handler(scanrepo_conv)
    app.add_handler(cleanrepo_conv)

    return app