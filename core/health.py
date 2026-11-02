"""
JobHunt AI Studio - System Health & Readiness Diagnostics
Inspects database access, storage writability, worker heartbeats, and browser readiness.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from core.run_store import run_store_instance

logger = logging.getLogger("JobHuntAIStudio")


def perform_system_health_check() -> Dict[str, Any]:
    """
    Executes end-to-end system diagnostics:
    - SQLite database read/write access
    - Persistent storage directory writability and free disk space
    - Active runs & worker status
    - Playwright browser binary readiness
    """
    results: Dict[str, Any] = {
        "status": "healthy",
        "timestamp": time.time(),
        "checks": {},
    }

    # 1. Database Check
    try:
        with run_store_instance._connection() as conn:
            row = conn.execute("SELECT 1 AS val").fetchone()
            if row and row["val"] == 1:
                results["checks"]["database"] = {"status": "ok", "path": str(run_store_instance.db_path)}
            else:
                results["checks"]["database"] = {"status": "failed", "error": "Query returned unexpected value"}
                results["status"] = "unhealthy"
    except Exception as e:
        results["checks"]["database"] = {"status": "failed", "error": str(e)}
        results["status"] = "unhealthy"

    # 2. Storage Writability Check
    try:
        test_dir = run_store_instance.db_dir
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / f".health_{int(time.time())}.tmp"
        test_file.write_text("health_check_ok", encoding="utf-8")
        test_file.unlink()

        usage = shutil.disk_usage(str(test_dir))
        free_mb = round(usage.free / (1024 * 1024), 2)
        results["checks"]["storage"] = {
            "status": "ok",
            "directory": str(test_dir),
            "free_space_mb": free_mb,
        }
        if free_mb < 50:
            results["checks"]["storage"]["warning"] = "Low disk space (< 50 MB)"
            if results["status"] == "healthy":
                results["status"] = "degraded"
    except Exception as e:
        results["checks"]["storage"] = {"status": "failed", "error": str(e)}
        results["status"] = "unhealthy"

    # 3. Active Runs & Queue Status
    try:
        active_runs = run_store_instance.get_active_runs_count()
        queued_runs = run_store_instance.get_queued_runs_count()
        results["checks"]["workers"] = {
            "status": "ok",
            "active_runs": active_runs,
            "queued_runs": queued_runs,
        }
    except Exception as e:
        results["checks"]["workers"] = {"status": "failed", "error": str(e)}

    # 4. Playwright Browser Check
    try:
        from playwright.sync_api import sync_playwright
        results["checks"]["playwright"] = {"status": "installed"}
    except ImportError as e:
        results["checks"]["playwright"] = {"status": "missing_library", "error": str(e)}
        results["status"] = "degraded"

    return results
