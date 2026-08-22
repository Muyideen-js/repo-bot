# GitHub PR Review Bot

A Telegram-controlled PR review service for contribution repositories such as
GrantFox and Drips Wave. It monitors new and existing pull requests, compares
each change with its linked issue, waits for CI, requests specific corrections,
and merges the exact reviewed commit when the change is complete.

## Review lifecycle

1. The maintainer connects a GitHub token with `/setup`.
2. `/addrepo` installs a signed GitHub webhook and immediately queues every
   existing open PR in the selected repository.
3. New commits and completed CI checks enqueue or wake an idempotent review job.
4. The worker refreshes the PR, requires `Closes #N`, `Fixes #N`, or
   `Resolves #N`, loads the issue and diff, and combines GitHub Checks with
   commit statuses.
5. Gemini returns a validated, fail-closed review decision.
6. Incomplete changes receive a review that tags the contributor and explains
   what to fix. Complete changes are approved and merged only if the PR still
   points to the reviewed SHA.
7. Temporary GitHub, network, CI, or Gemini failures are retried from the
   durable database queue. Final outcomes are sent to Telegram and audited.

## Telegram commands

- `/setup` — connect or rotate the GitHub token
- `/addrepo` — monitor a repository and scan all existing PRs
- `/scanrepo` — discover all open PRs and retry only new or failed commits
- `/listrepos` — list monitored repositories
- `/removerepo` — remove the webhook and stop monitoring
- `/status` — show monitored repositories and queued work
- `/cleanrepo` — delete legacy bot error comments
- `/help` and `/cancel`

## Required environment variables

Copy `.env.example` to `.env` for local development. Never commit `.env`.

```env
TELEGRAM_BOT_TOKEN=...
ENCRYPTION_KEY=...
GEMINI_API_KEY=...
GEMINI_REQUEST_INTERVAL_SECONDS=15
PUBLIC_URL=https://your-service.example
GITHUB_WEBHOOK_SECRET=a-random-value-at-least-32-characters-long
DATABASE_URL=postgresql://user:password@host/database
```

Generate the Fernet encryption key with:

```bash
python scripts/generate_key.py
```

The GitHub token needs access to repository contents, issues, pull requests,
checks/statuses, webhook administration, and merging. A classic PAT generally
uses `repo` and `write:repo_hook`; use narrowly scoped fine-grained permissions
where the target organization supports them.

## Run locally

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --reload --port 8000
```

Use an HTTPS tunnel as `PUBLIC_URL` when testing GitHub webhooks locally.

## Deploy on Render

1. Create a managed PostgreSQL database. Do not use the default SQLite file in
   production because a Render filesystem is ephemeral.
2. Set every required environment variable in the web service. Use the external
   PostgreSQL URL as `DATABASE_URL`.
3. Use `pip install -r requirements.txt` as the build command.
4. Use `uvicorn app.main:app --host 0.0.0.0 --port $PORT` as the start command.
5. Run exactly one web-service instance. Telegram long polling and the queue
   worker are intentionally single-instance in this version.
6. Verify `GET /health`, then open Telegram and run `/setup` and `/addrepo`.

On startup, the service updates previously registered hooks so they also receive
CI completion events.

## Safety properties

- GitHub tokens are encrypted at rest with Fernet.
- Webhook verification fails closed if the secret is absent or incorrect.
- Webhook requests return quickly after storing durable work.
- Each repository/PR/SHA combination is idempotent in the queue.
- Active, queued, failed, and completed work survives restarts with PostgreSQL.
- CI cannot pass while any check is still running.
- A push during review invalidates the decision.
- The merge API is pinned to the reviewed SHA and still respects GitHub branch
  protection.
- Model responses must match the expected types; invalid output never approves.

AI review reduces maintainer work but is not a mathematical proof of correctness.
Keep branch protection, required CI, and repository-level merge rules enabled.
