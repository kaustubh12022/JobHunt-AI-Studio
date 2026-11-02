"""
JobHunt AI Studio - Session & Workspace Manager
Enforces strict multi-tenant isolation, automatic temp file cleanup,
fingerprint-validated batch checkpoints, and background janitor maintenance.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("JobHuntAIStudio")

BASE_TEMP_DIR = Path(__file__).parent.parent / "temp" / "sessions"


class SessionManager:
    """
    Manages isolated user sessions, working directories, temporary file cleanup,
    and batch checkpointing with input fingerprint verification.
    """

    def __init__(self, base_dir: Optional[Path] = None, ttl_seconds: int = 3600):
        self.base_dir = base_dir or BASE_TEMP_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds

    def get_session_dir(self, session_id: str) -> Path:
        """Returns or creates the private, isolated workspace for a given session."""
        clean_id = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
        if not clean_id:
            clean_id = uuid.uuid4().hex[:12]
        session_path = self.base_dir / clean_id
        session_path.mkdir(parents=True, exist_ok=True)
        return session_path

    def get_resumes_dir(self, session_id: str) -> Path:
        """Returns the private PDF output folder for a given session."""
        r_dir = self.get_session_dir(session_id) / "resumes"
        r_dir.mkdir(parents=True, exist_ok=True)
        return r_dir

    def cleanup_session(self, session_id: str):
        """Immediately cleans up a specific user's temporary files."""
        try:
            s_dir = self.get_session_dir(session_id)
            if s_dir.exists():
                shutil.rmtree(s_dir, ignore_errors=True)
                logger.info(f"Cleaned up session workspace: {session_id}")
        except Exception as e:
            logger.warning(f"Error cleaning up session {session_id}: {e}")

    def cleanup_expired_sessions(self):
        """
        Background Janitor: purges all session folders older than TTL (default 1 hour).
        Prevents disk clutter on hosted environments (e.g. Render).
        """
        if not self.base_dir.exists():
            return
        now = time.time()
        for item in self.base_dir.iterdir():
            if item.is_dir():
                try:
                    mtime = item.stat().st_mtime
                    if now - mtime > self.ttl_seconds:
                        shutil.rmtree(item, ignore_errors=True)
                        logger.info(f"Purged expired session directory: {item.name}")
                except Exception as e:
                    logger.debug(f"Janitor could not inspect/purge {item}: {e}")

    # ── Batch Recovery Checkpointing ──

    def get_checkpoint_file(self, session_id: str) -> Path:
        return self.get_session_dir(session_id) / "batch_checkpoint.json"

    def save_checkpoint(
        self,
        session_id: str,
        total_jobs: int,
        completed_jobs: List[Dict[str, Any]],
        threshold: int,
        model: str,
        provider: str,
        fingerprint: Optional[str] = None,
    ):
        """Saves current batch progress with optional input fingerprint validation."""
        cp_file = self.get_checkpoint_file(session_id)
        data = {
            "total_jobs": total_jobs,
            "completed_count": len(completed_jobs),
            "completed_jobs": completed_jobs,
            "threshold": threshold,
            "model": model,
            "provider": provider,
            "fingerprint": fingerprint or "",
            "timestamp": time.time(),
        }
        with open(cp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_checkpoint(
        self,
        session_id: str,
        expected_fingerprint: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Loads saved batch checkpoint if available.
        If expected_fingerprint is provided, invalidates checkpoint if inputs changed.
        """
        cp_file = self.get_checkpoint_file(session_id)
        if not cp_file.exists():
            return None
        try:
            with open(cp_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if expected_fingerprint and data.get("fingerprint"):
                if data["fingerprint"] != expected_fingerprint:
                    logger.warning(f"Checkpoint fingerprint mismatch for session {session_id}. Resetting.")
                    self.clear_checkpoint(session_id)
                    return None
            return data
        except Exception as e:
            logger.warning(f"Could not load checkpoint for {session_id}: {e}")
            return None

    def clear_checkpoint(self, session_id: str):
        """Deletes checkpoint when a batch is finished or reset."""
        cp_file = self.get_checkpoint_file(session_id)
        if cp_file.exists():
            try:
                cp_file.unlink()
            except Exception:
                pass


session_manager = SessionManager()
