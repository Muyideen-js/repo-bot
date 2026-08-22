"""
All GitHub API interactions: fetch PR data, post reviews, register webhooks, merge.
"""
import re
import hmac
import hashlib
import os
import httpx
import logging
import asyncio
from typing import Optional


GITHUB_API = "https://api.github.com"
logger = logging.getLogger(__name__)
CLOSES_PATTERN = re.compile(
    r"(?:closes|fixes|resolves)\s+#(\d+)",
    re.IGNORECASE,
)


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """Verify the request actually came from GitHub using the webhook secret."""
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


def _headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_pr(token: str, repo: str, pr_number: int) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers=_headers(token),
        )
        r.raise_for_status()
        return r.json()


async def get_pr_diff(token: str, repo: str, pr_number: int) -> str:
    """Fetch a PR diff, falling back to the files API for oversized diffs."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers={**_headers(token), "Accept": "application/vnd.github.diff"},
        )
        if r.status_code == 200:
            return r.text
        if r.status_code not in (406, 422):
            r.raise_for_status()

    logger.warning(
        "Raw diff unavailable for %s#%s (HTTP %s); using files API fallback",
        repo, pr_number, r.status_code,
    )
    files = await get_pr_files(token, repo, pr_number)
    sections = []
    for file in files:
        filename = file.get("filename", "unknown")
        previous = file.get("previous_filename")
        header = [
            f"diff --git a/{previous or filename} b/{filename}",
            f"status: {file.get('status', 'modified')}",
            f"changes: +{file.get('additions', 0)} -{file.get('deletions', 0)}",
        ]
        patch = file.get("patch")
        if patch:
            header.append(patch)
        else:
            header.append("[Patch unavailable: binary or too large for GitHub's files API]")
        sections.append("\n".join(header))
    if not sections:
        raise RuntimeError(f"GitHub returned no changed files for {repo}#{pr_number}")
    return "\n\n".join(sections)


async def get_pr_files(token: str, repo: str, pr_number: int) -> list:
    files = []
    async with httpx.AsyncClient() as client:
        page = 1
        while True:
            r = await client.get(
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files",
                headers=_headers(token),
                params={"per_page": 100, "page": page},
            )
            r.raise_for_status()
            batch = r.json()
            files.extend(batch)
            if len(batch) < 100:
                return files
            page += 1


async def get_issue(token: str, repo: str, issue_number: int) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{issue_number}",
            headers=_headers(token),
        )
        r.raise_for_status()
        return r.json()


async def get_ci_status(token: str, repo: str, sha: str) -> str:
    """Return combined Checks API and commit-status state."""
    async with httpx.AsyncClient() as client:
        checks_response = await client.get(
            f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs",
            headers=_headers(token),
            params={"per_page": 100, "page": 1},
        )
        status_response = await client.get(
            f"{GITHUB_API}/repos/{repo}/commits/{sha}/status",
            headers=_headers(token),
        )
        checks_data = checks_response.json() if checks_response.status_code == 200 else {}
        runs = checks_data.get("check_runs", [])
        total_runs = checks_data.get("total_count", len(runs))
        page = 2
        while len(runs) < total_runs:
            page_response = await client.get(
                f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs",
                headers=_headers(token),
                params={"per_page": 100, "page": page},
            )
            if page_response.status_code != 200:
                return "pending"
            batch = page_response.json().get("check_runs", [])
            if not batch:
                return "pending"
            runs.extend(batch)
            page += 1
        status_data = status_response.json() if status_response.status_code == 200 else {}
        has_commit_statuses = bool(
            status_data.get("total_count") or status_data.get("statuses")
        )
        combined_state = status_data.get("state") if has_commit_statuses else None
        if any(run.get("status") != "completed" for run in runs):
            return "pending"
        failed = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
        if any(run.get("conclusion") in failed for run in runs):
            return "failure"
        if combined_state in ("failure", "error"):
            return "failure"
        if combined_state == "pending":
            return "pending"
        if runs or combined_state == "success":
            return "success"
        return "none"


def extract_issue_number(pr_body: str) -> Optional[int]:
    """
    Parse PR description for 'Closes #123', 'Fixes #123', 'Resolves #123'.
    Returns the issue number or None if not found.
    """
    if not pr_body:
        return None
    match = CLOSES_PATTERN.search(pr_body)
    return int(match.group(1)) if match else None


async def post_review_comment(
    token: str,
    repo: str,
    pr_number: int,
    body: str,
    approve: bool = False,
) -> None:
    """Post a review — either APPROVE or REQUEST_CHANGES with a comment."""
    event = "APPROVE" if approve else "REQUEST_CHANGES"
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews",
            headers=_headers(token),
            json={"body": body, "event": event},
        )
        r.raise_for_status()


async def post_issue_comment(
    token: str, repo: str, pr_number: int, body: str
) -> None:
    """Post a plain comment on the PR (used for missing Closes # notice)."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
            headers=_headers(token),
            json={"body": body},
        )
        r.raise_for_status()


async def merge_pr(
    token: str, repo: str, pr_number: int, pr_title: str, expected_sha: str
) -> bool:
    """Merge only if the PR still points at the reviewed commit."""
    async with httpx.AsyncClient() as client:
        r = await client.put(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/merge",
            headers=_headers(token),
            json={
                "sha": expected_sha,
                "commit_title": f"Merge PR #{pr_number}: {pr_title}",
                "merge_method": "merge",  # regular merge — preserves all commits
            },
        )
        return r.status_code == 200


async def get_pr_merge_state(
    token: str, repo: str, pr_number: int, attempts: int = 4
) -> str:
    """Wait briefly for GitHub's asynchronous mergeability calculation."""
    state = "unknown"
    for attempt in range(attempts):
        pr = await get_pr(token, repo, pr_number)
        state = pr.get("mergeable_state") or "unknown"
        if state != "unknown" and pr.get("mergeable") is not None:
            return state
        if attempt < attempts - 1:
            await asyncio.sleep(1)
    return state


async def register_webhook(token: str, repo: str, webhook_url: str, secret: str) -> str:
    """Register a webhook on the repo to receive PR events. Returns webhook ID."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{GITHUB_API}/repos/{repo}/hooks",
            headers=_headers(token),
            json={
                "name": "web",
                "active": True,
                "events": ["pull_request", "check_run", "check_suite", "status"],
                "config": {
                    "url": webhook_url,
                    "content_type": "json",
                    "secret": secret,
                    "insecure_ssl": "0",
                },
            },
        )
        r.raise_for_status()
        return str(r.json()["id"])


async def update_webhook(
    token: str, repo: str, webhook_id: str, webhook_url: str, secret: str
) -> None:
    """Keep an existing hook aligned with the required event set."""
    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"{GITHUB_API}/repos/{repo}/hooks/{webhook_id}",
            headers=_headers(token),
            json={
                "active": True,
                "events": ["pull_request", "check_run", "check_suite", "status"],
                "config": {
                    "url": webhook_url,
                    "content_type": "json",
                    "secret": secret,
                    "insecure_ssl": "0",
                },
            },
        )
        r.raise_for_status()


async def delete_webhook(token: str, repo: str, webhook_id: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{GITHUB_API}/repos/{repo}/hooks/{webhook_id}",
            headers=_headers(token),
        )


async def get_open_prs(token: str, repo: str, limit: int = 5, page: int = 1) -> list:
    """Fetch open PRs on a repo — paginated, newest first."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls",
            headers=_headers(token),
            params={
                "state": "open",
                "per_page": limit,
                "page": page,
                "sort": "created",
                "direction": "desc",
            },
        )
        r.raise_for_status()
        return r.json()


async def get_all_open_prs(token: str, repo: str) -> list:
    """Fetch all currently open pull requests."""
    pull_requests = []
    page = 1
    while True:
        batch = await get_open_prs(token, repo, limit=100, page=page)
        pull_requests.extend(batch)
        if len(batch) < 100:
            return pull_requests
        page += 1


async def delete_bot_comments(token: str, repo: str, texts_to_delete: list[str]) -> int:
    """
    Find and delete all issue/PR comments containing any of the given texts.
    Returns the number of comments deleted.
    """
    deleted = 0
    async with httpx.AsyncClient() as client:
        # Fetch all comments on the repo (up to 100 pages)
        page = 1
        while True:
            r = await client.get(
                f"{GITHUB_API}/repos/{repo}/issues/comments",
                headers=_headers(token),
                params={"per_page": 100, "page": page},
            )
            if r.status_code != 200:
                break
            comments = r.json()
            if not comments:
                break
            for comment in comments:
                body = comment.get("body", "")
                if any(text in body for text in texts_to_delete):
                    del_r = await client.delete(
                        f"{GITHUB_API}/repos/{repo}/issues/comments/{comment['id']}",
                        headers=_headers(token),
                    )
                    if del_r.status_code == 204:
                        deleted += 1
            if len(comments) < 100:
                break
            page += 1
    return deleted


async def validate_token(token: str) -> Optional[str]:
    """Check if the token is valid. Returns GitHub username or None."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/user",
            headers=_headers(token),
        )
        if r.status_code == 200:
            return r.json().get("login")
        return None


async def check_repo_access(token: str, repo: str) -> bool:
    """Check if the token has access to push/merge on the repo."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}",
            headers=_headers(token),
        )
        if r.status_code != 200:
            return False
        perms = r.json().get("permissions", {})
        return perms.get("push", False) or perms.get("admin", False)
