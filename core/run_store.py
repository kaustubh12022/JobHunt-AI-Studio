"""
JobHunt AI Studio - Persistent SQLite RunStore
Provides ACID transactional persistence for multi-tenant batch runs,
immutable input snapshots, worker leases, stage checkpoints, and artifact tracking.
Designed for persistent disk storage on hosted services like Render.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.models import JobInput, JobResult, RunConfig, RunStatus, Stage, StageEvent
from core.security import verify_recovery_code

logger = logging.getLogger("JobHuntAIStudio")

# Default persistent store directory
DEFAULT_STORE_DIR = Path(os.environ.get("PERSISTENT_DATA_DIR", os.environ.get("DATA_DIR", Path(__file__).parent.parent / "temp" / "store")))


from contextlib import contextmanager


class RunStore:
    """
    SQLite-backed transactional store for batch runs, jobs, checkpoints, leases, and artifacts.
    Supports Render persistent disks and handles server restart recovery.
    """

    def __init__(self, db_dir: Optional[Path] = None):
        self.db_dir = Path(db_dir) if db_dir else DEFAULT_STORE_DIR
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "jobhunt_studio.db"
        self._init_database()
        try:
            self.recover_interrupted_runs()
            self.cleanup_expired_runs()
        except Exception as e:
            logger.warning(f"Error during startup recovery/cleanup: {e}")

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _get_connection(self):
        """Deprecated: use self._connection() context manager."""
        return self._connection()

    def _init_database(self):
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    recovery_code_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    score_threshold INTEGER NOT NULL,
                    max_resumes INTEGER NOT NULL,
                    is_gemini_free INTEGER NOT NULL,
                    total_jobs INTEGER NOT NULL,
                    completed_count INTEGER DEFAULT 0,
                    verified_resumes_count INTEGER DEFAULT 0,
                    input_fingerprint TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    usage_json TEXT,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    raw_index INTEGER NOT NULL,
                    job_input_json TEXT NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, job_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS stage_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    job_id TEXT,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    audit_passed INTEGER,
                    metadata_json TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
                CREATE INDEX IF NOT EXISTS idx_runs_expires ON runs(expires_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id, raw_index);
                CREATE INDEX IF NOT EXISTS idx_events_run ON stage_events(run_id, created_at);
            """)

    def get_run_artifacts_dir(self, run_id: str) -> Path:
        """Isolated artifacts directory for a specific run."""
        clean_id = "".join(c for c in run_id if c.isalnum() or c in ("-", "_"))
        d = self.db_dir / "runs" / clean_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create_run(
        self,
        config: RunConfig,
        profile: Dict[str, Any],
        jobs: List[JobInput],
        recovery_code_hash: str,
        input_fingerprint: str,
        ttl_seconds: int = 86400,  # 24 hours
    ) -> str:
        now = time.time()
        expires_at = now + ttl_seconds
        run_id = config.run_id

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, recovery_code_hash, status, provider, model,
                    score_threshold, max_resumes, is_gemini_free, total_jobs,
                    completed_count, verified_resumes_count, input_fingerprint,
                    profile_json, config_json, usage_json, lease_owner, lease_expires_at,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    recovery_code_hash,
                    RunStatus.PENDING.value,
                    config.provider,
                    config.model,
                    config.score_threshold,
                    config.max_resumes,
                    1 if config.is_gemini_free else 0,
                    len(jobs),
                    0,
                    0,
                    input_fingerprint,
                    json.dumps(profile),
                    json.dumps(config.to_dict()),
                    json.dumps({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
                    None,
                    None,
                    now,
                    now,
                    expires_at,
                ),
            )

            job_records = [
                (
                    run_id,
                    job.job_id,
                    job.raw_row_index,
                    json.dumps(job.to_dict()),
                    None,
                    "Pending",
                    Stage.VALIDATE.value,
                    now,
                )
                for job in jobs
            ]

            conn.executemany(
                """
                INSERT INTO jobs (run_id, job_id, raw_index, job_input_json, result_json, status, stage, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                job_records,
            )

        logger.info(f"Created persistent run {run_id} with {len(jobs)} jobs.")
        return run_id

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return None
            return dict(row)

    def verify_recovery(self, run_id: str, plain_recovery_code: str) -> bool:
        run = self.get_run(run_id)
        if not run:
            return False
        stored_hash = run.get("recovery_code_hash", "")
        return verify_recovery_code(plain_recovery_code, stored_hash)

    def acquire_lease(self, run_id: str, worker_id: str, duration_sec: int = 60) -> bool:
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE run_id = ?
                  AND (lease_owner IS NULL OR lease_owner = ? OR lease_expires_at < ?)
                  AND status IN (?, ?, ?)
                """,
                (
                    worker_id,
                    now + duration_sec,
                    now,
                    run_id,
                    worker_id,
                    now,
                    RunStatus.PENDING.value,
                    RunStatus.RUNNING.value,
                    RunStatus.AWAITING_CREDENTIALS.value,
                ),
            )
            return cursor.rowcount > 0

    def renew_lease(self, run_id: str, worker_id: str, duration_sec: int = 60) -> bool:
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET lease_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND lease_owner = ?
                """,
                (now + duration_sec, now, run_id, worker_id),
            )
            return cursor.rowcount > 0

    def release_lease(self, run_id: str, worker_id: str):
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND lease_owner = ?
                """,
                (now, run_id, worker_id),
            )

    def update_run_status(self, run_id: str, status: RunStatus, error: Optional[str] = None):
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status.value, now, run_id),
            )

    def get_job_inputs(self, run_id: str) -> List[JobInput]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT job_input_json FROM jobs WHERE run_id = ? ORDER BY raw_index ASC",
                (run_id,),
            ).fetchall()
            return [JobInput.from_dict(json.loads(r["job_input_json"])) for r in rows]

    def save_job_result(self, run_id: str, result: JobResult, stage: Stage):
        now = time.time()
        result_dict = result.to_dict()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET result_json = ?, status = ?, stage = ?, updated_at = ?
                WHERE run_id = ? AND job_id = ?
                """,
                (json.dumps(result_dict), result.status, stage.value, now, run_id, result.job_id),
            )

            # Update runs completed_count and verified_resumes_count
            completed = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE run_id = ? AND result_json IS NOT NULL",
                (run_id,),
            ).fetchone()["c"]

            verified = conn.execute(
                """
                SELECT COUNT(*) AS c FROM jobs
                WHERE run_id = ? AND result_json IS NOT NULL
                  AND json_extract(result_json, '$.pdf_path') IS NOT NULL
                  AND json_extract(result_json, '$.audit_result.passed') = 1
                """,
                (run_id,),
            ).fetchone()["c"]

            # Update usage tokens
            token_stats = conn.execute(
                """
                SELECT
                    COALESCE(SUM(json_extract(result_json, '$.prompt_tokens')), 0) AS p_tok,
                    COALESCE(SUM(json_extract(result_json, '$.completion_tokens')), 0) AS c_tok
                FROM jobs WHERE run_id = ? AND result_json IS NOT NULL
                """,
                (run_id,),
            ).fetchone()
            p_tok = int(token_stats["p_tok"])
            c_tok = int(token_stats["c_tok"])
            usage_json = json.dumps({"prompt_tokens": p_tok, "completion_tokens": c_tok, "total_tokens": p_tok + c_tok})

            conn.execute(
                """
                UPDATE runs
                SET completed_count = ?, verified_resumes_count = ?, usage_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (completed, verified, usage_json, now, run_id),
            )

    def get_completed_job_results(self, run_id: str) -> List[JobResult]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT result_json FROM jobs WHERE run_id = ? AND result_json IS NOT NULL ORDER BY raw_index ASC",
                (run_id,),
            ).fetchall()
            return [JobResult.from_dict(json.loads(r["result_json"])) for r in rows]

    def record_stage_event(self, event: StageEvent):
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO stage_events (event_id, run_id, job_id, stage, status, message, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id or str(uuid.uuid4().hex[:12]),
                    event.run_id,
                    event.job_id,
                    event.stage,
                    event.status,
                    event.message,
                    json.dumps(event.details),
                    now,
                ),
            )

    def get_stage_events(self, run_id: str, limit: int = 100) -> List[StageEvent]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM stage_events WHERE run_id = ? ORDER BY created_at DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
            events = []
            for r in rows:
                events.append(
                    StageEvent(
                        event_id=r["event_id"],
                        run_id=r["run_id"],
                        job_id=r["job_id"],
                        stage=r["stage"],
                        status=r["status"],
                        message=r["message"],
                        timestamp=r["created_at"],
                        details=json.loads(r["details_json"] or "{}"),
                    )
                )
            return events

    def record_artifact(
        self,
        run_id: str,
        artifact_type: str,
        file_path: str,
        audit_passed: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        now = time.time()
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        art_id = str(uuid.uuid4().hex[:12])
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (artifact_id, run_id, artifact_type, file_path, file_size, audit_passed, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    art_id,
                    run_id,
                    artifact_type,
                    str(file_path),
                    file_size,
                    1 if audit_passed else 0,
                    json.dumps(metadata or {}),
                    now,
                ),
            )

    def get_artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def recover_interrupted_runs(self) -> int:
        """
        Server restart recovery:
        Finds runs that were left in 'running' state whose worker lease has expired.
        Transitions them to 'awaiting_credentials' so the user can supply API keys and resume.
        """
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE status = ? AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                """,
                (RunStatus.AWAITING_CREDENTIALS.value, now, RunStatus.RUNNING.value, now),
            )
            count = cursor.rowcount
            if count > 0:
                logger.info(f"Server restart recovery: transitioned {count} interrupted runs to awaiting_credentials.")
            return count

    def cleanup_expired_runs(self, max_age_hours: int = 24) -> int:
        """
        Janitor: purges expired runs older than max_age_hours, unless currently held by an active lease.
        """
        now = time.time()
        cleaned = 0
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE expires_at < ?
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                """,
                (now, now),
            ).fetchall()

            for r in rows:
                r_id = r["run_id"]
                conn.execute("DELETE FROM runs WHERE run_id = ?", (r_id,))
                art_dir = self.get_run_artifacts_dir(r_id)
                if art_dir.exists():
                    shutil.rmtree(art_dir, ignore_errors=True)
                cleaned += 1
                logger.info(f"Cleaned up expired run: {r_id}")
        return cleaned

    def delete_run(self, run_id: str):
        """Immediately deletes a run and its filesystem artifacts."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        art_dir = self.get_run_artifacts_dir(run_id)
        if art_dir.exists():
            shutil.rmtree(art_dir, ignore_errors=True)
        logger.info(f"Explicitly deleted run {run_id} and its artifacts.")

    def get_active_runs_count(self) -> int:
        now = time.time()
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM runs
                WHERE status = ? AND lease_expires_at > ?
                """,
                (RunStatus.RUNNING.value, now),
            ).fetchone()
            return row["c"]

    def get_queued_runs_count(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM runs WHERE status IN (?, ?)",
                (RunStatus.PENDING.value, RunStatus.AWAITING_CREDENTIALS.value),
            ).fetchone()
            return row["c"]

    def find_run_by_fingerprint(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        """Look up an existing unexpired run by input fingerprint."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE input_fingerprint = ? AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
                (fingerprint, time.time()),
            ).fetchone()
            return dict(row) if row else None


# Global default store instance
run_store_instance = RunStore()
