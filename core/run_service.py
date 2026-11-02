"""
JobHunt AI Studio - RunService & Background Worker Orchestrator
Decoupled batch orchestration service executing:
Validate -> Score -> Tailor -> Render -> Check -> Package.
Enforces:
- In-memory credential isolation (zero API key persistence to disk or DB)
- Cryptographic recovery code authentication for all operations
- Lease management and server restart recovery
- Worker execution independent of Streamlit UI / session lifecycle
- Truthful ATS verification gates
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.ai_engine import ScopedAIClient
from core.ats_verifier import audit_resume_pdf
from core.models import ArtifactResult, JobInput, JobResult, RunConfig, RunStatus, Stage, StageEvent
from core.packager import build_batch_zip_bytes
from core.pdf_generator import render_and_audit_pdf
from core.resume_tailor import tailor_resume_async
from core.run_store import RunStore, run_store_instance
from core.scorer import score_job_async
from core.security import (
    MAX_ACTIVE_RUNS,
    MAX_QUEUED_RUNS,
    build_safe_pdf_filename,
    compute_input_fingerprint,
    generate_recovery_code,
    hash_recovery_code,
)
from core.summary_reporter import generate_summary_csv, render_summary_html

logger = logging.getLogger("JobHuntAIStudio")


class RunService:
    """
    Manages batch job lifecycle, bounded execution workers, and recovery.
    Streamlit UI interacts exclusively through this service.
    """

    def __init__(self, store: Optional[RunStore] = None):
        self.store = store or run_store_instance
        # Ephemeral run-scoped memory: credentials are NEVER persisted to disk or DB
        self._credentials: Dict[str, List[str]] = {}
        self._pause_flags: Dict[str, bool] = {}
        self._active_tasks: Dict[str, asyncio.Task] = {}

    def check_storage_pressure(self, min_free_mb: int = 100) -> bool:
        """Returns True if available disk storage is below threshold."""
        try:
            usage = shutil.disk_usage(str(self.store.db_dir))
            free_mb = usage.free / (1024 * 1024)
            return free_mb < min_free_mb
        except Exception:
            return False

    def create_run(
        self,
        config: RunConfig,
        profile: Dict[str, Any],
        jobs: List[JobInput],
    ) -> Tuple[str, str]:
        """
        Creates a new immutable run snapshot.
        Enforces queue saturation limits, storage pressure, and returns (run_id, plain_recovery_code).
        Only the SHA-256 hash of the recovery code is persisted.
        """
        if self.check_storage_pressure():
            raise RuntimeError("Storage pressure: available disk space is too low to accept new runs.")

        queued_count = self.store.get_queued_runs_count()
        if queued_count >= MAX_QUEUED_RUNS:
            raise RuntimeError(f"Queue saturation: system already has {queued_count} queued runs.")

        plain_code = generate_recovery_code()
        code_hash = hash_recovery_code(plain_code)

        input_fingerprint = compute_input_fingerprint(
            jobs=[j.to_dict() for j in jobs],
            profile=profile,
            threshold=config.score_threshold,
            max_resumes=config.max_resumes,
            model=config.model,
        )

        run_id = self.store.create_run(
            config=config,
            profile=profile,
            jobs=jobs,
            recovery_code_hash=code_hash,
            input_fingerprint=input_fingerprint,
        )

        self.store.record_stage_event(
            StageEvent(
                event_id=uuid.uuid4().hex[:12],
                run_id=run_id,
                stage=Stage.VALIDATE.value,
                status="success",
                message=f"Batch run created with {len(jobs)} jobs.",
            )
        )

        return run_id, plain_code

    def start_or_resume_run(
        self,
        run_id: str,
        recovery_code: str,
        api_keys: List[str],
        background: bool = True,
        expected_fingerprint: Optional[str] = None,
    ) -> Optional[asyncio.Task]:
        """
        Starts or resumes a run after verifying the recovery code.
        Injects API keys into run-scoped memory only.
        If expected_fingerprint is provided, verifies that batch inputs have not changed.
        """
        if not self.store.verify_recovery(run_id, recovery_code):
            raise PermissionError("Invalid recovery code for this run.")

        run = self.store.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found.")

        if expected_fingerprint:
            stored_fp = run.get("input_fingerprint")
            if stored_fp and stored_fp != expected_fingerprint:
                raise ValueError("Changed inputs detected: inputs do not match checkpoint fingerprint. A new run must be created.")

        clean_keys = [k.strip() for k in api_keys if k and k.strip()]
        if not clean_keys:
            raise ValueError("At least one valid API key is required to start processing.")

        self._credentials[run_id] = clean_keys
        self._pause_flags[run_id] = False

        worker_id = f"worker_{uuid.uuid4().hex[:8]}"

        if background:
            task = asyncio.create_task(self._run_worker(run_id, worker_id))
            self._active_tasks[run_id] = task
            return task
        else:
            # Synchronous execution
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                task = asyncio.create_task(self._run_worker(run_id, worker_id))
                self._active_tasks[run_id] = task
                return task
            else:
                asyncio.run(self._run_worker(run_id, worker_id))
                return None

    def pause_run(self, run_id: str, recovery_code: str):
        """Signals worker to pause execution after the current job finishes."""
        if not self.store.verify_recovery(run_id, recovery_code):
            raise PermissionError("Invalid recovery code for this run.")
        self._pause_flags[run_id] = True
        logger.info(f"Pause requested for run {run_id}.")

    def delete_run(self, run_id: str, recovery_code: str):
        """Immediately aborts execution, purges all database state and files, and revokes access."""
        if not self.store.verify_recovery(run_id, recovery_code):
            raise PermissionError("Invalid recovery code for this run.")

        self._pause_flags[run_id] = True
        if run_id in self._active_tasks:
            task = self._active_tasks.pop(run_id)
            if not task.done():
                task.cancel()

        self._credentials.pop(run_id, None)
        self.store.delete_run(run_id)
        logger.info(f"Deleted run {run_id} and all its artifacts.")

    def retry_failed_jobs(
        self,
        run_id: str,
        recovery_code: str,
        api_keys: List[str],
        background: bool = True,
    ) -> Optional[asyncio.Task]:
        """Re-runs failed jobs for a given batch."""
        if not self.store.verify_recovery(run_id, recovery_code):
            raise PermissionError("Invalid recovery code for this run.")

        return self.start_or_resume_run(
            run_id=run_id,
            recovery_code=recovery_code,
            api_keys=api_keys,
            background=background,
        )

    def package_run_artifacts(self, run_id: str, recovery_code: str) -> ArtifactResult:
        """
        Independently builds or retries packaging the batch ZIP bundle
        from already generated and verified PDFs.
        """
        if not self.store.verify_recovery(run_id, recovery_code):
            raise PermissionError("Invalid recovery code for this run.")

        run = self.store.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found.")

        completed_results = self.store.get_completed_job_results(run_id)
        res_dicts = [r.to_dict() for r in completed_results]

        profile_json = run.get("profile_json", "{}")
        import json
        profile = json.loads(profile_json)
        p_info = profile.get("personal_information", {})
        cand_name = f"{p_info.get('name', 'Candidate')} {p_info.get('surname', '')}".strip()

        # Build summary reports
        summary_csv = generate_summary_csv(res_dicts)
        summary_html = render_summary_html(
            results=res_dicts,
            candidate_name=cand_name,
            threshold=run.get("score_threshold", 70),
        )

        # Collect verified PDFs only
        verified_pdf_paths = [
            r["pdf_path"]
            for r in res_dicts
            if r.get("pdf_path") and (r.get("audit_result") or {}).get("passed") is True
        ]

        zip_bytes = build_batch_zip_bytes(
            pdf_paths=verified_pdf_paths,
            summary_csv_str=summary_csv,
            summary_html_str=summary_html,
        )

        art_dir = self.store.get_run_artifacts_dir(run_id)
        zip_path = art_dir / "jobhunt_tailored_batch.zip"
        with open(zip_path, "wb") as f:
            f.write(zip_bytes)

        self.store.record_artifact(
            run_id=run_id,
            artifact_type="zip",
            file_path=str(zip_path),
            audit_passed=True,
            metadata={"verified_resumes": len(verified_pdf_paths)},
        )

        return ArtifactResult(
            run_id=run_id,
            zip_path=str(zip_path),
            zip_bytes=zip_bytes,
            summary_csv_str=summary_csv,
            summary_html_str=summary_html,
            pdf_paths=verified_pdf_paths,
            verified_count=len(verified_pdf_paths),
            failed_count=len(res_dicts) - len(verified_pdf_paths),
        )

    def get_run_status(self, run_id: str, recovery_code: Optional[str] = None) -> Dict[str, Any]:
        """Returns the public or detailed status of a run."""
        run = self.store.get_run(run_id)
        if not run:
            return {"status": "not_found"}

        verified = False
        if recovery_code:
            verified = self.store.verify_recovery(run_id, recovery_code)

        events = self.store.get_stage_events(run_id, limit=20)
        recent_event = events[0].to_dict() if events else None

        data = {
            "run_id": run["run_id"],
            "status": run["status"],
            "total_jobs": run["total_jobs"],
            "completed_count": run["completed_count"],
            "verified_resumes_count": run["verified_resumes_count"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "recent_event": recent_event,
            "is_authorized": verified,
        }

        if verified:
            data["config"] = json.loads(run["config_json"])
            data["usage"] = json.loads(run["usage_json"] or "{}")
            data["events"] = [e.to_dict() for e in events]
            data["completed_jobs"] = [r.to_dict() for r in self.store.get_completed_job_results(run_id)]

        return data

    get_status = get_run_status

    async def _run_worker(self, run_id: str, worker_id: str):
        """
        Bounded background worker loop processing jobs stage by stage:
        Validate -> Score -> Tailor -> Render -> Check -> Package.
        """
        if not self.store.acquire_lease(run_id, worker_id, duration_sec=60):
            logger.warning(f"Worker {worker_id} could not acquire lease for run {run_id}.")
            return

        self.store.update_run_status(run_id, RunStatus.RUNNING)
        logger.info(f"Worker {worker_id} started execution for run {run_id}.")

        run = self.store.get_run(run_id)
        import json
        config_data = json.loads(run["config_json"])
        config = RunConfig.from_dict(config_data)
        profile = json.loads(run["profile_json"])
        jobs = self.store.get_job_inputs(run_id)
        api_keys = self._credentials.get(run_id, [])

        art_dir = self.store.get_run_artifacts_dir(run_id)
        resumes_dir = art_dir / "resumes"
        resumes_dir.mkdir(parents=True, exist_ok=True)

        scoped_client = None
        if api_keys:
            try:
                scoped_client = ScopedAIClient(
                    provider=config.provider,
                    api_keys=api_keys,
                    model=config.model,
                    rpm_limit=15 if config.is_gemini_free else 60,
                )
            except Exception as e:
                self.store.update_run_status(run_id, RunStatus.FAILED)
                self.store.record_stage_event(
                    StageEvent(
                        event_id=uuid.uuid4().hex[:12],
                        run_id=run_id,
                        stage=Stage.VALIDATE.value,
                        status="failed",
                        message=f"Configuration error: {e}",
                    )
                )
                self.store.release_lease(run_id, worker_id)
                self._credentials.pop(run_id, None)
                return

        # Load already completed results
        existing_results = {r.job_id: r for r in self.store.get_completed_job_results(run_id)}
        verified_count = sum(
            1 for r in existing_results.values()
            if r.pdf_path and (r.audit_result or {}).get("passed") is True
        )

        try:
            for job in jobs:
                # 1. Check if paused
                if self._pause_flags.get(run_id, False):
                    logger.info(f"Run {run_id} paused at job {job.job_id}.")
                    self.store.update_run_status(run_id, RunStatus.PAUSED)
                    self.store.record_stage_event(
                        StageEvent(
                            event_id=uuid.uuid4().hex[:12],
                            run_id=run_id,
                            job_id=job.job_id,
                            stage=Stage.SCORE.value,
                            status="paused",
                            message=f"Batch paused at {job.company} — {job.title}.",
                        )
                    )
                    return

                # Renew worker lease
                self.store.renew_lease(run_id, worker_id, duration_sec=60)

                # 2. Check if job is already completely finished
                if job.job_id in existing_results:
                    prev = existing_results[job.job_id]
                    is_verified = bool(prev.pdf_path and (prev.audit_result or {}).get("passed") is True)
                    is_below_thresh = bool(prev.scoring_status == "success" and prev.match_score < config.score_threshold)
                    is_cap_reached = bool(prev.status == "Cap Reached")
                    if is_verified or is_below_thresh or is_cap_reached:
                        continue

                # 3. Stage: Score (skip re-scoring if scoring already succeeded in a prior run)
                job_result = None
                if job.job_id in existing_results and existing_results[job.job_id].scoring_status == "success":
                    job_result = existing_results[job.job_id]
                else:
                    self.store.record_stage_event(
                        StageEvent(
                            event_id=uuid.uuid4().hex[:12],
                            run_id=run_id,
                            job_id=job.job_id,
                            stage=Stage.SCORE.value,
                            status="start",
                            message=f"Scoring {job.company} — {job.title}",
                        )
                    )

                    job_dict = job.to_dict()
                    scored_res = await score_job_async(
                        job=job_dict,
                        profile=profile,
                        model=config.model,
                        provider=config.provider,
                        api_keys=api_keys,
                        ai_client=scoped_client,
                    )

                    job_result = JobResult.from_dict(scored_res)

                    # Check if scoring failed
                    if job_result.scoring_status == "failed":
                        self.store.save_job_result(run_id, job_result, Stage.SCORE)
                        self.store.record_stage_event(
                            StageEvent(
                                event_id=uuid.uuid4().hex[:12],
                                run_id=run_id,
                                job_id=job.job_id,
                                stage=Stage.SCORE.value,
                                status="failed",
                                message=f"Scoring failed for {job.company}: {job_result.error}",
                            )
                        )
                        continue

                # 4. Threshold & Resume Limit Check
                score = job_result.match_score
                if score >= config.score_threshold and verified_count < config.max_resumes:
                    # 5. Stage: Tailor
                    self.store.record_stage_event(
                        StageEvent(
                            event_id=uuid.uuid4().hex[:12],
                            run_id=run_id,
                            job_id=job.job_id,
                            stage=Stage.TAILOR.value,
                            status="start",
                            message=f"Tailoring resume for {job.company} (Match: {score}%)",
                        )
                    )

                    try:
                        tailored_res = await tailor_resume_async(
                            job=job_result.to_dict(),
                            profile=profile,
                            model=config.model,
                            provider=config.provider,
                            api_keys=api_keys,
                            ai_client=scoped_client,
                        )
                        job_result.tailored_resume = tailored_res

                        # 6. Stage: Render (Playwright with semaphore concurrency=1)
                        self.store.record_stage_event(
                            StageEvent(
                                event_id=uuid.uuid4().hex[:12],
                                run_id=run_id,
                                job_id=job.job_id,
                                stage=Stage.RENDER.value,
                                status="start",
                                message=f"Rendering PDF for {job.company}",
                            )
                        )

                        p_info = profile.get("personal_information", {})
                        cand_name = f"{p_info.get('name', 'Candidate')}_{p_info.get('surname', '')}".strip('_')
                        custom_fn = build_safe_pdf_filename(job.company, job.title, job.job_id, cand_name)

                        pdf_path, audit_res = render_and_audit_pdf(
                            tailored_resume=tailored_res,
                            output_dir=resumes_dir,
                            job=job_result.to_dict(),
                            custom_filename=custom_fn,
                        )

                        job_result.pdf_path = pdf_path
                        job_result.pdf_filename = os.path.basename(pdf_path)
                        job_result.audit_result = audit_res

                        # 7. Stage: Check (ATS Audit Gate)
                        if audit_res.get("passed", False):
                            verified_count += 1
                            job_result.status = "Tailored & Verified"
                            self.store.record_stage_event(
                                StageEvent(
                                    event_id=uuid.uuid4().hex[:12],
                                    run_id=run_id,
                                    job_id=job.job_id,
                                    stage=Stage.CHECK.value,
                                    status="success",
                                    message=f"Verified 1-page ATS compliant: {os.path.basename(pdf_path)}",
                                )
                            )
                        else:
                            job_result.status = "Audit Failed: " + ", ".join(audit_res.get("errors", ["Flagged"]))[:40]
                            self.store.record_stage_event(
                                StageEvent(
                                    event_id=uuid.uuid4().hex[:12],
                                    run_id=run_id,
                                    job_id=job.job_id,
                                    stage=Stage.CHECK.value,
                                    status="failed",
                                    message=f"PDF failed ATS check: {audit_res.get('errors')}",
                                )
                            )

                    except Exception as exc:
                        failed_stage = Stage.RENDER.value if job_result.tailored_resume else Stage.TAILOR.value
                        job_result.status = f"{failed_stage} Error: {str(exc)[:40]}"
                        job_result.error = str(exc)
                        self.store.record_stage_event(
                            StageEvent(
                                event_id=uuid.uuid4().hex[:12],
                                run_id=run_id,
                                job_id=job.job_id,
                                stage=failed_stage,
                                status="failed",
                                message=f"{failed_stage} failed for {job.company}: {exc}",
                            )
                        )
                else:
                    job_result.pdf_path = None
                    job_result.pdf_filename = None
                    job_result.status = "Below Threshold" if score < config.score_threshold else "Cap Reached"
                    self.store.record_stage_event(
                        StageEvent(
                            event_id=uuid.uuid4().hex[:12],
                            run_id=run_id,
                            job_id=job.job_id,
                            stage=Stage.SCORE.value,
                            status="skipped",
                            message=f"{job.company} — {job_result.status} (Match {score}%)",
                        )
                    )

                self.store.save_job_result(run_id, job_result, Stage.CHECK)

            # 8. Stage: Package
            self.store.record_stage_event(
                StageEvent(
                    event_id=uuid.uuid4().hex[:12],
                    run_id=run_id,
                    stage=Stage.PACKAGE.value,
                    status="start",
                    message="Packaging verified PDFs, CSV summary, and HTML digest into ZIP bundle.",
                )
            )

            # Retrieve dummy or plain recovery code verification internally for final package
            completed_results = self.store.get_completed_job_results(run_id)
            res_dicts = [r.to_dict() for r in completed_results]
            p_info = profile.get("personal_information", {})
            cand_name = f"{p_info.get('name', 'Candidate')} {p_info.get('surname', '')}".strip()

            summary_csv = generate_summary_csv(res_dicts)
            summary_html = render_summary_html(
                results=res_dicts,
                candidate_name=cand_name,
                threshold=config.score_threshold,
            )

            verified_pdfs = [
                r["pdf_path"]
                for r in res_dicts
                if r.get("pdf_path") and (r.get("audit_result") or {}).get("passed") is True
            ]

            zip_bytes = build_batch_zip_bytes(
                pdf_paths=verified_pdfs,
                summary_csv_str=summary_csv,
                summary_html_str=summary_html,
            )

            zip_path = art_dir / "jobhunt_tailored_batch.zip"
            with open(zip_path, "wb") as f:
                f.write(zip_bytes)

            self.store.record_artifact(
                run_id=run_id,
                artifact_type="zip",
                file_path=str(zip_path),
                audit_passed=True,
                metadata={"verified_resumes": len(verified_pdfs)},
            )

            self.store.update_run_status(run_id, RunStatus.COMPLETED)
            self.store.record_stage_event(
                StageEvent(
                    event_id=uuid.uuid4().hex[:12],
                    run_id=run_id,
                    stage=Stage.PACKAGE.value,
                    status="success",
                    message=f"Batch complete. {len(verified_pdfs)} verified single-page resumes ready.",
                )
            )

        except Exception as e:
            logger.error(f"Worker {worker_id} crashed for run {run_id}: {e}", exc_info=True)
            self.store.update_run_status(run_id, RunStatus.FAILED)
            self.store.record_stage_event(
                StageEvent(
                    event_id=uuid.uuid4().hex[:12],
                    run_id=run_id,
                    stage=Stage.PACKAGE.value,
                    status="failed",
                    message=f"Run interrupted: {e}",
                )
            )
        finally:
            self.store.release_lease(run_id, worker_id)
            self._credentials.pop(run_id, None)
            self._active_tasks.pop(run_id, None)


# Global default service instance
run_service_instance = RunService()
