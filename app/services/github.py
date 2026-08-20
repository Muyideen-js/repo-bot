"""
All GitHub API interactions: fetch PR data, post reviews, register webhooks, merge.
"""
import re
import hmac
import hashlib
import os
import httpx
from typing import Optional


GITHUB_API = "https://api.github.com"
CLOSES_PATTERN = re.compile(
    r"(?:closes|fixes|resolves)\s+#(\d+)",
    re.IGNORECASE,
)


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """Verify the request actually came from GitHub using the webhook secret."""
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        return True  # skip verification in dev if secret not set
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
    """Fetch the raw unified diff of the PR."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers={**_headers(token), "Accept": "application/vnd.github.diff"},
        )
        r.raise_for_status()
        return r.text


async def get_pr_files(token: str, repo: str, pr_number: int) -> list:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files",
            headers=_headers(token),
        )
        r.raise_for_status()
        return r.json()


async def get_issue(token: str, repo: str, issue_number: int) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{issue_number}",
            headers=_headers(token),
        )
        r.raise_for_status()
        return r.json()


async def get_ci_status(token: str, repo: str, sha: str) -> str:
    """Return combined CI conclusion: success | failure | pending | none."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs",
            headers=_headers(token),
        )
        if r.status_code != 200:
            return "none"
        data = r.json()
        runs = data.get("check_runs", [])
        if not runs:
            return "none"
        statuses = [run["conclusion"] for run in runs if run["conclusion"]]
        if all(s == "success" for s in statuses):
            return "success"
        if any(s in ("failure", "cancelled", "timed_out") for s in statuses):
            return "failure"
        return "pending"


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


async def merge_pr(token: str, repo: str, pr_number: int, pr_title: str) -> bool:
    """Merge the PR using regular merge strategy."""
    async with httpx.AsyncClient() as client:
        r = await client.put(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/merge",
            headers=_headers(token),
            json={
                "commit_title": f"Merge PR #{pr_number}: {pr_title}",
                "merge_method": "merge",  # regular merge — preserves all commits
            },
        )
        return r.status_code == 200


async def register_webhook(token: str, repo: str, webhook_url: str, secret: str) -> str:
    """Register a webhook on the repo to receive PR events. Returns webhook ID."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{GITHUB_API}/repos/{repo}/hooks",
            headers=_headers(token),
            json={
                "name": "web",
                "active": True,
                "events": ["pull_request"],
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


async def delete_webhook(token: str, repo: str, webhook_id: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{GITHUB_API}/repos/{repo}/hooks/{webhook_id}",
            headers=_headers(token),
        )


async def get_open_prs(token: str, repo: str) -> list:
    """Fetch all open PRs on a repo."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls",
            headers=_headers(token),
            params={"state": "open", "per_page": 50},
        )
        r.raise_for_status()
        return r.json()


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
