"""Fast GitHub webhook receiver that only validates and queues work."""
import json

from fastapi import APIRouter, HTTPException, Request

from app.services import github as gh
from app.services.review_queue import enqueue_pr, wake_jobs_for_sha

router = APIRouter()
PR_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review", "edited"}


@router.post("/webhook/github")
async def github_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not gh.verify_webhook_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    event_type = request.headers.get("X-GitHub-Event", "")
    repo_full_name = (payload.get("repository") or {}).get("full_name")
    if not repo_full_name:
        return {"status": "ignored", "event": event_type}

    queued = 0
    if event_type == "pull_request" and payload.get("action") in PR_ACTIONS:
        queued = int(await enqueue_pr(
            repo_full_name,
            payload.get("pull_request") or {},
            force=payload.get("action") in {"edited", "ready_for_review"},
        ))
    elif event_type in {"check_run", "check_suite"}:
        container = payload.get(event_type) or {}
        for pr in container.get("pull_requests") or []:
            queued += int(await enqueue_pr(repo_full_name, pr, force=True))
    elif event_type == "status" and payload.get("sha"):
        await wake_jobs_for_sha(repo_full_name, payload["sha"])

    return {"status": "accepted", "event": event_type, "queued": queued}
