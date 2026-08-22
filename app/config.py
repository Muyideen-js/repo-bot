"""Runtime configuration validation."""
import os
from urllib.parse import urlparse


REQUIRED_SETTINGS = (
    "TELEGRAM_BOT_TOKEN",
    "ENCRYPTION_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "PUBLIC_URL",
    "GITHUB_WEBHOOK_SECRET",
)


def validate_settings() -> None:
    missing = [name for name in REQUIRED_SETTINGS if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required environment settings: {', '.join(missing)}")

    public_url = os.environ["PUBLIC_URL"].rstrip("/")
    parsed = urlparse(public_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("PUBLIC_URL must be a public HTTPS URL")
    if os.environ["GITHUB_WEBHOOK_SECRET"] == "default_secret":
        raise RuntimeError("GITHUB_WEBHOOK_SECRET must be a strong random value")
    if len(os.environ["GITHUB_WEBHOOK_SECRET"]) < 32:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET must be at least 32 characters")
