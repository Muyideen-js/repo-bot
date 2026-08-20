"""
Telegram bot interface.
Users set up the bot here: provide GitHub token, add repos, remove repos.

Commands:
  /start   — welcome + instructions
  /setup   — begin GitHub token setup
  /addrepo — register a repo for PR review
  /listrepos — show all registered repos
  /removerepo — unregister a repo
  /status  — show bot status for your account
  /help    — show all commands
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
WAITING_FOR_TOKEN = 1
WAITING_FOR_REPO = 2
WAITING_FOR_REMOVE_REPO = 3
WAITING_FOR_SCAN_CHOICE = 4


# ── /start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the *GitHub PR Review Bot*!\n\n"
        "I automatically review Pull Requests on your repos:\n"
        "• ✅ If a PR fully solves the linked issue → I merge it\n"
        "• ❌ If it's incomplete → I tag the contributor with exactly what to fix\n"
        "• ⚠️ If the PR has no `Closes #issue` → I block it and explain why\n\n"
        "To get started:\n"
        "1. Run /setup to connect your GitHub account\n"
        "2. Run /addrepo to register a repo\n"
        "3. That's it — I'll handle the rest!\n\n"
        "Run /help to see all commands.",
        parse_mode="Markdown",
    )


# ── /help ─────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Available Commands:*\n\n"
        "/setup — Connect your GitHub account\n"
        "/addrepo — Add a repo to monitor\n"
        "/scanrepo — Review all existing open PRs on a repo\n"
        "/listrepos — See your registered repos\n"
        "/removerepo — Remove a repo\n"
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
        "• `repo` (full repo access for reading PRs, issues, merging)\n"
        "• `write:repo_hook` (to register webhooks)\n\n"
        "👉 Create one at: https://github.com/settings/tokens/new\n\n"
        "Once you have it, paste it here.\n"
        "Your token is encrypted before storage — only you can use it.\n\n"
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

    # Validate token with GitHub
    await update.message.reply_text("⏳ Validating your token with GitHub...")
    github_username = await gh.validate_token(token)
    if not github_username:
        await update.message.reply_text(
            "❌ That token doesn't seem to be valid. Please check it and try /setup again."
        )
        return ConversationHandler.END

    # Encrypt and save
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
        "Now run /addrepo to register a repo for PR review monitoring.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── /addrepo ──────────────────────────────────────────────────────────────────
async def addrepo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user or not user.setup_complete:
            await update.message.reply_text("Please run /setup first to connect your GitHub account.")
            return ConversationHandler.END

    await update.message.reply_text(
        "📦 Which repo do you want to monitor?\n\n"
        "Send the full repo name, e.g.:\n"
        "`yusuf/my-project`\n"
        "`GrantFox/some-repo`\n\n"
        "I'll check that you have push access before registering.\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_REPO


async def receive_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_name = update.message.text.strip().strip("/")
    telegram_id = str(update.effective_user.id)

    if "/" not in repo_name:
        await update.message.reply_text(
            "❌ That doesn't look right. Format should be `owner/repo-name`.",
            parse_mode="Markdown",
        )
        return WAITING_FOR_REPO

    await update.message.reply_text(f"⏳ Checking access to `{repo_name}`...", parse_mode="Markdown")

    async with AsyncSessionLocal() as db:
        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        if not user:
            await update.message.reply_text("Please run /setup first.")
            return ConversationHandler.END

        from app.services.crypto import decrypt_token
        token = decrypt_token(user.github_token_encrypted)

        # Check if already registered
        existing = await db.execute(
            select(Repo).where(
                Repo.telegram_id == telegram_id,
                Repo.full_name == repo_name,
            )
        )
        if existing.scalar_one_or_none():
            await update.message.reply_text(f"⚠️ `{repo_name}` is already registered.", parse_mode="Markdown")
            return ConversationHandler.END

        # Check push access
        has_access = await gh.check_repo_access(token, repo_name)
        if not has_access:
            await update.message.reply_text(
                f"❌ Your token doesn't have push access to `{repo_name}`.\n"
                "Make sure you're a collaborator or maintainer on that repo.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        # Register webhook
        public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
        webhook_url = f"{public_url}/webhook/github"
        secret = os.getenv("GITHUB_WEBHOOK_SECRET", "default_secret")

        try:
            webhook_id = await gh.register_webhook(token, repo_name, webhook_url, secret)
        except Exception as e:
            await update.message.reply_text(
                f"❌ Failed to register webhook: {str(e)}\n"
                "Make sure your token has `write:repo_hook` permission."
            )
            return ConversationHandler.END

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
        "• I'll check for `Closes #issue` in the description\n"
        "• Review the diff against the issue requirements using AI\n"
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

    lines = [f"• `{r.full_name}`" for r in repos]
    await update.message.reply_text(
        "*Which repo do you want to remove?*\n\n"
        + "\n".join(lines)
        + "\n\nSend the full name (e.g. `owner/repo`). Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_REMOVE_REPO


async def receive_remove_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_name = update.message.text.strip()
    telegram_id = str(update.effective_user.id)

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
            await update.message.reply_text(f"❌ `{repo_name}` not found in your repos.", parse_mode="Markdown")
            return ConversationHandler.END

        # Delete the webhook from GitHub
        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        if user and repo.webhook_id:
            from app.services.crypto import decrypt_token
            token = decrypt_token(user.github_token_encrypted)
            try:
                await gh.delete_webhook(token, repo_name, repo.webhook_id)
            except Exception:
                pass  # Webhook may already be gone

        repo.active = False
        await db.commit()

    await update.message.reply_text(
        f"✅ Removed `{repo_name}`. I'll no longer monitor PRs on that repo.",
        parse_mode="Markdown",
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

    # Store repos list in context for the next step
    context.user_data["scan_repos"] = [r.full_name for r in repos]

    lines = [f"{i+1}. `{r.full_name}`" for i, r in enumerate(repos)]
    await update.message.reply_text(
        "🔍 *Which repo do you want to scan?*\n\n"
        + "\n".join(lines)
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
    await update.message.reply_text(
        f"⏳ Scanning `{repo_name}` for open PRs...",
        parse_mode="Markdown",
    )

    async with AsyncSessionLocal() as db:
        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        if not user:
            await update.message.reply_text("Please run /setup first.")
            return ConversationHandler.END

        from app.services.crypto import decrypt_token
        from app.services.pr_reviewer import scan_all_open_prs

        token = decrypt_token(user.github_token_encrypted)
        results = await scan_all_open_prs(
            token=token,
            telegram_id=telegram_id,
            repo_full_name=repo_name,
            db=db,
        )

    if not results:
        await update.message.reply_text(
            f"✅ No open PRs found on `{repo_name}`.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Build summary report
    icons = {
        "MERGED": "✅",
        "COMMENTED": "🔍",
        "SKIPPED": "⚠️",
        "RATE_LIMITED": "⏳",
        "ERROR": "❌",
    }
    labels = {
        "MERGED": "Merged",
        "COMMENTED": "Needs changes (contributor tagged)",
        "SKIPPED": "Skipped — no `Closes #issue`",
        "RATE_LIMITED": "AI limit hit — PR notified",
        "ERROR": "Error — manual review needed",
    }

    lines = []
    for r in results:
        icon = icons.get(r["outcome"], "❓")
        label = labels.get(r["outcome"], r["outcome"])
        lines.append(f"{icon} *PR #{r['pr_number']}* — {r['pr_title'][:40]}\n   ↳ {label}")

    summary = "\n\n".join(lines)
    await update.message.reply_text(
        f"*Scan complete — {repo_name}*\n"
        f"_{len(results)} PR(s) reviewed_\n\n"
        + summary,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


def build_telegram_app() -> Application:
    """Build and return the configured Telegram application."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    app = Application.builder().token(bot_token).build()

    # Setup conversation
    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup", setup_start)],
        states={WAITING_FOR_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Add repo conversation
    addrepo_conv = ConversationHandler(
        entry_points=[CommandHandler("addrepo", addrepo_start)],
        states={WAITING_FOR_REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_repo)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Remove repo conversation
    removerepo_conv = ConversationHandler(
        entry_points=[CommandHandler("removerepo", removerepo_start)],
        states={WAITING_FOR_REMOVE_REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_remove_repo)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Scan repo conversation
    scanrepo_conv = ConversationHandler(
        entry_points=[CommandHandler("scanrepo", scanrepo_start)],
        states={WAITING_FOR_SCAN_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_scan_choice)]},
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

    return app
