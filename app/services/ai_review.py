"""
AI review engine using Gemini 2.0 Flash.
Compares the issue requirements against the PR diff and returns a structured decision.
Handles rate limits gracefully with retry support.
"""
import os
import httpx
import json
import asyncio
import logging

logger = logging.getLogger(__name__)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

# Custom exceptions
class AIRateLimitError(Exception):
    """Raised when Gemini hits its daily/minute quota."""
    pass

class AIReviewError(Exception):
    """Raised for other AI errors."""
    pass


async def review_pr(
    issue_title: str,
    issue_body: str,
    pr_title: str,
    pr_body: str,
    diff: str,
    ci_status: str,
    contributor: str,
    retries: int = 2,
) -> dict:
    """
    Ask Gemini to review the PR against the issue.
    Retries up to `retries` times on transient errors.
    Raises AIRateLimitError if quota is exceeded.

    Returns:
        {
          "approved": bool,
          "summary": str,
          "missing": list[str],
          "comment": str
        }
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    # Truncate very large diffs to stay within token limits
    max_diff_chars = 12_000
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + "\n\n... [diff truncated for review]"

    prompt = f"""You are a senior code reviewer bot. Your job is to decide whether a GitHub Pull Request completely and correctly solves the linked issue.

=== ISSUE ===
Title: {issue_title}
Body:
{issue_body or "(no description)"}

=== PULL REQUEST ===
Title: {pr_title}
Description:
{pr_body or "(no description)"}

=== CI STATUS ===
{ci_status}

=== CODE DIFF ===
{diff}

=== YOUR TASK ===
1. Read the issue carefully. Identify every requirement, bug, or feature it describes.
2. Read the PR diff. Determine if EVERY requirement from the issue is addressed.
3. Check if tests are included where appropriate.
4. Check if the CI is passing (if CI status is "failure", that is a blocker).
5. Check for obvious regressions or new bugs introduced.

Respond ONLY with a JSON object in this exact format (no markdown, no explanation outside JSON):
{{
  "approved": true or false,
  "summary": "one sentence verdict",
  "missing": ["list of specific things missing or wrong — empty array if approved"],
  "comment": "the full comment to post on GitHub (be specific, actionable, professional)"
}}

If approved, the comment should congratulate and confirm the merge.
If not approved, the comment must:
- Tag the contributor as @{contributor}
- List each missing or incorrect thing clearly and specifically
- Tell them exactly what to change or add
- Be respectful and constructive
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
        },
    }

    last_error = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{GEMINI_URL}?key={api_key}",
                    json=payload,
                )

            # Rate limit hit (429) or quota exceeded (400 with specific message)
            if r.status_code == 429:
                raise AIRateLimitError("Gemini API daily/minute quota exceeded.")

            if r.status_code == 400:
                body = r.json()
                msg = str(body).lower()
                if "quota" in msg or "rate" in msg or "limit" in msg:
                    raise AIRateLimitError("Gemini API quota exceeded.")

            r.raise_for_status()
            data = r.json()

            raw_text = (
                data["candidates"][0]["content"]["parts"][0]["text"]
                .strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )

            result = json.loads(raw_text)
            return result

        except AIRateLimitError:
            # Do not retry rate limit errors — raise immediately
            raise

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            if attempt < retries:
                wait = 2 ** attempt  # exponential backoff: 1s, 2s
                logger.warning(f"Gemini request failed (attempt {attempt+1}), retrying in {wait}s: {e}")
                await asyncio.sleep(wait)
            continue

        except json.JSONDecodeError:
            # AI returned malformed JSON — safe fallback, no retry needed
            return {
                "approved": False,
                "summary": "AI review could not be parsed. Manual review required.",
                "missing": ["AI response was malformed."],
                "comment": (
                    f"@{contributor} — The automated review encountered a parsing issue. "
                    "A maintainer will review this PR manually."
                ),
            }

        except Exception as e:
            last_error = e
            if attempt < retries:
                wait = 2 ** attempt
                logger.warning(f"Gemini error (attempt {attempt+1}), retrying in {wait}s: {e}")
                await asyncio.sleep(wait)
            continue

    # All retries exhausted
    logger.error(f"Gemini review failed after {retries+1} attempts: {last_error}")
    raise AIReviewError(f"AI review failed after {retries+1} attempts: {last_error}")
