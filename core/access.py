"""
JobHunt AI Studio - Owner Access Gate
The studio is private: a visitor must present the server-owned owner passphrase
before any workspace, profile or run data is rendered. The gate fails closed when
no passphrase is configured.
"""

from __future__ import annotations

import hmac
import logging

from core.settings import access_key

logger = logging.getLogger("JobHuntAIStudio")

MIN_PASSCODE_LENGTH = 12


def access_configured() -> bool:
    """True only when a sufficiently strong owner passphrase is configured."""
    return len(access_key()) >= MIN_PASSCODE_LENGTH


def verify_access(candidate: str) -> bool:
    """Constant-time comparison of the presented passphrase."""
    expected = access_key()
    provided = (candidate or "").strip()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
