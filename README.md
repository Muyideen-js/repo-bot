# GitHub PR Review Bot

An autonomous GitHub PR review bot with a Telegram interface. Users set up the bot via Telegram — it monitors their repos, reviews PRs using AI (Gemini 2.0 Flash), and either merges or requests changes automatically.

---

## How It Works

1. User DMs the bot on Telegram and runs `/setup`
2. User provides their GitHub Personal Access Token (encrypted before storage)
3. User runs `/addrepo owner/repo-name` to register a repo
4. The bot registers a webhook on that repo via GitHub API
5. When a PR is opened or updated:
   - **No `Closes #issue`?** → Bot blocks merge, tags contributor, explains what to add
   - **CI failing?** → Bot waits or requests fixes
   - **AI reviews diff vs issue** → Approves & merges if complete, or requests specific changes
6. Bot notifies the repo owner on Telegram with the outcome

---

## Setup

### 1. Create a Telegram Bot
- Message [@BotFather](https://t.me/BotFather) on Telegram
- Run `/newbot` and follow the steps
- Copy the `TELEGRAM_BOT_TOKEN`

### 2. Get a Gemini API Key
- Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
- Create an API key
- Copy the `GEMINI_API_KEY`

### 3. Generate an Encryption Key
```bash
python scripts/generate_key.py
```
Copy the output as your `ENCRYPTION_KEY`.

### 4. Set Environment Variables
Copy `.env.example` to `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ENCRYPTION_KEY=your_generated_fernet_key
GEMINI_API_KEY=your_gemini_api_key
PUBLIC_URL=https://your-bot.railway.app   # set after deployment
GITHUB_WEBHOOK_SECRET=any_random_string_you_choose
```

### 5. Deploy to Railway (Recommended)

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add all environment variables in Railway's dashboard
4. Copy the generated Railway URL into `PUBLIC_URL` in your env vars
5. Redeploy

### 6. Run Locally (Development)
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your values
# Set PUBLIC_URL to your ngrok URL for local testing:
# ngrok http 8000
uvicorn app.main:app --reload --port 8000
```

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and instructions |
| `/setup` | Connect your GitHub account |
| `/addrepo` | Register a repo for PR monitoring |
| `/listrepos` | List your monitored repos |
| `/removerepo` | Remove a repo |
| `/status` | Check your account setup |
| `/help` | Show all commands |

---

## GitHub Token Permissions Required

When creating your Personal Access Token (classic):
- ✅ `repo` — full repository access (read PRs, issues, diffs, merge)
- ✅ `write:repo_hook` — register webhooks on repos

---

## PR Review Rules

The bot will **NOT merge** if:
- PR description has no `Closes #N`, `Fixes #N`, or `Resolves #N`
- CI checks are failing
- The AI determines the PR doesn't fully solve the linked issue
- The PR introduces obvious regressions

The bot **WILL merge** (regular merge) if:
- PR links a valid issue with `Closes #N`
- CI is passing (or no CI configured)
- AI confirms every issue requirement is addressed in the diff
- No regressions detected

---

## Project Structure

```
pr-review-bot/
├── app/
│   ├── main.py                  # FastAPI app + Telegram bot startup
│   ├── handlers/
│   │   ├── telegram_bot.py      # Telegram commands and setup wizard
│   │   └── webhook.py           # GitHub webhook receiver
│   ├── services/
│   │   ├── github.py            # All GitHub API calls
│   │   ├── ai_review.py         # Gemini 2.0 Flash review engine
│   │   ├── pr_reviewer.py       # Main PR review orchestrator
│   │   └── crypto.py            # Token encryption/decryption
│   └── models/
│       └── database.py          # SQLAlchemy models (User, Repo, PRLog)
├── scripts/
│   └── generate_key.py          # One-time encryption key generator
├── requirements.txt
├── Procfile
├── railway.toml
└── .env.example
```

---

## Security

- GitHub tokens are **encrypted with Fernet** before database storage
- Webhook payloads are **signature-verified** against `GITHUB_WEBHOOK_SECRET`
- Token messages from users are **deleted immediately** after receipt
- Least-privilege: bot only needs `repo` + `write:repo_hook` permissions
