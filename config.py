"""
config.py — Application configuration
Reads settings from environment variables (via .env file).
"""
import os
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()


class Config:
    # ── Flask ──────────────────────────────────────────────────
    SECRET_KEY: str = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")
    DEBUG: bool = os.environ.get("FLASK_DEBUG", "0") == "1"

    # ── File uploads ───────────────────────────────────────────
    MAX_CONTENT_MB: int = int(os.environ.get("MAX_CONTENT_LENGTH_MB", "16"))
    MAX_CONTENT_LENGTH: int = MAX_CONTENT_MB * 1024 * 1024
    ALLOWED_EXTENSIONS: set = {"pdf", "txt"}
    UPLOAD_FOLDER: str = os.path.join(os.path.dirname(__file__), "uploads")

    # ── Database ───────────────────────────────────────────────
    DATABASE_PATH: str = os.environ.get(
        "DATABASE_PATH",
        os.path.join(os.path.dirname(__file__), "database", "smart_notes.db"),
    )

    # ── AI provider ────────────────────────────────────────────
    # Set AI_PROVIDER=google or AI_PROVIDER=openai; anything else = fallback only
    AI_PROVIDER: str = os.environ.get("AI_PROVIDER", "none").lower()

    # Google Gemini
    GOOGLE_API_KEY: str = os.environ.get("GOOGLE_API_KEY", "")
    GOOGLE_MODEL: str = os.environ.get("GOOGLE_MODEL", "gemini-1.5-flash")

    # OpenAI
    OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
