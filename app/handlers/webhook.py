"""
FastAPI route that receives GitHub PR webhook events.
GitHub sends a POST here whenever a PR is opened, updated, or closed.
"""
import json
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.services import github as gh
from app.services.pr_reviewer import handle_pr_event

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhook/github")
async def github_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Receive and process GitHub PR webhook events."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    # Verify it's actually from GitHub
    if not gh.verify_webhook_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type != "pull_request":
        # We only care about PR events
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Process asynchronously — return 200 immediately so GitHub doesn't retry
    try:
        await handle_pr_event(payload, db)
    except Exception as e:
        logger.error(f"Error handling PR event: {e}", exc_info=True)
        # Still return 200 to GitHub — errors are logged, not retried
        return {"status": "error", "detail": str(e)}

    return {"status": "ok"}
