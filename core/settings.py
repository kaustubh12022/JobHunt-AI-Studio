"""
JobHunt AI Studio - Locked Studio Settings
Single-owner, DeepSeek-only configuration for Kaustubh Kale.

Everything that identifies the studio, its candidate profile, its AI engine and
its batch policy is fixed here (or read from server-owned environment secrets).
None of it can be changed from the web interface by a visitor.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = ROOT / ".env"

try:
    _FILE_ENV = dotenv_values(_ENV_PATH) if _ENV_PATH.is_file() else {}
except Exception:  # pragma: no cover - malformed .env must never crash the app
    _FILE_ENV = {}


def get_setting(name: str, default: str = "") -> str:
    """Read a server-owned setting (process env wins, then .env, then default)."""
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        value = _FILE_ENV.get(name, default)
    return str(value or "").strip()


def deepseek_api_key() -> str:
    """The single DeepSeek key owned by the studio operator."""
    return get_setting("DEEPSEEK_API_KEY")


def access_key() -> str:
    """Passphrase required to open the private studio."""
    return get_setting("STUDIO_ACCESS_KEY")


# ── Locked owner identity ──────────────────────────────────────────────
OWNER_NAME = "Kaustubh Kale"
OWNER_EMAIL = "kaustubh.kale.work@gmail.com"
STUDIO_NAME = "JobHunt AI Studio"
PROFILE_PATH = ROOT / "data" / "profile.yaml"

# ── Locked AI engine (DeepSeek only) ──────────────────────────────────
PROVIDER = "deepseek"
MODEL = "deepseek-flash"
MODEL_LABEL = "DeepSeek V4.1 Flash"
REVIEW_MODEL = "deepseek-flash"
API_BASE_URL = "https://api.deepseek.com"
REQUEST_TIMEOUT = 120.0
CONCURRENCY = 6
MAX_REVIEWS_IN_FLIGHT = 2

# ── Locked batch policy ───────────────────────────────────────────────
SCORE_THRESHOLD = 70
MAX_RESUMES = 20
MAX_DRAFTS = 3
APPROVAL_THRESHOLD = 85

# ── Optional token pricing (operator can supply it server-side) ────────
def input_price_per_million() -> Optional[float]:
    return _as_float(get_setting("DEEPSEEK_INPUT_PER_M"))


def output_price_per_million() -> Optional[float]:
    return _as_float(get_setting("DEEPSEEK_OUTPUT_PER_M"))


def _as_float(value: str) -> Optional[float]:
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None
