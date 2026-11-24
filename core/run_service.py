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
import threading
from dataclasses import asdict
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from core.ai_engine import DeepSeekClient
from core.ai_errors import CredentialsRequired, RunPaused
from core.company_context import collect_company_context
from core.resume_review import ResumeAttempt, approved_pdf, approved_pdf_bytes, pdf_digest, review_resume_async
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
from core.settings import (
    CONCURRENCY,
    MAX_REVIEWS_IN_FLIGHT,
    MODEL,
    PROVIDER,
    REVIEW_MODEL,
    deepseek_api_key,
)
from core.summary_reporter import generate_summary_csv, render_summary_html

logger = logging.getLogger("JobHuntAIStudio")
_review_slots = threading.BoundedSemaphore(MAX_REVIEWS_IN_FLIGHT)



class RunService:
    """
    Manages batch job lifecycle, bounded execution workers, and recovery.
    Streamlit UI interacts exclusively through this service.
    """

    def __init__(self, store: Optional[RunStore] = None):
        self.store = store or run_store_instance
        # Ephemeral run-scoped memory: credentials are NEVER persisted to disk or DB
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

        if config.reviewer_model and (config.provider != PROVIDER or not 1 <= config.max_drafts <= 5
                                      or not 0 <= config.approval_threshold <= 100):
            raise ValueError("Reviewed runs require the locked DeepSeek engine and a valid review policy.")
        plain_code = generate_recovery_code()
        code_hash = hash_recovery_code(plain_code)

        input_fingerprint = compute_input_fingerprint(
            jobs=[j.to_dict() for j in jobs],
            profile=profile,
            threshold=config.score_threshold,
            max_resumes=config.max_resumes,
            model=config.model,
            review_policy={
                "provider": config.provider, "model": config.reviewer_model,
                "threshold": config.approval_threshold, "max_drafts": config.max_drafts,
            } if config.reviewer_model else None,
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
        background: bool = True,
        expected_fingerprint: Optional[str] = None,
    ) -> Optional[asyncio.Task]:
        """
        Starts or resumes a run after verifying the recovery code.
        The DeepSeek key is read from server configuration and held in memory only.
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

        api_key = deepseek_api_key()
        if not api_key:
            raise CredentialsRequired(
                "The DeepSeek API key is not configured on this server. Set DEEPSEEK_API_KEY and restart."
            )

        config = RunConfig.from_dict(json.loads(run["config_json"]))
        worker_id = f"worker_{uuid.uuid4().hex[:8]}"
        if not self.store.acquire_lease(run_id, worker_id, max_active=MAX_ACTIVE_RUNS):
            raise RuntimeError("This run is already active or both worker slots are occupied.")
        self._pause_flags[run_id] = False
        self.store.update_run_status(run_id, RunStatus.RUNNING)
        worker = self._run_worker(run_id, worker_id, api_key, config)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if not background:
                try:
                    asyncio.run(worker)
                finally:
                    self.store.release_lease(run_id, worker_id)
                return None
            worker.close()
            self.store.release_lease(run_id, worker_id)
            self.store.update_run_status(run_id, RunStatus.AWAITING_CREDENTIALS)
            raise RuntimeError("Use BatchRunner to start background runs from a synchronous caller.") from None
        task = asyncio.create_task(worker)
        self._active_tasks[run_id] = task
        return task

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

        run = self.store.get_run(run_id)
        if run and (run.get("lease_expires_at") or 0) > time.time():
            raise RuntimeError("Pause the active run before deleting its files.")
        self.store.delete_run(run_id)
        logger.info(f"Deleted run {run_id} and all its artifacts.")

    def retry_failed_jobs(
        self,
        run_id: str,
        recovery_code: str,
        background: bool = True,
    ) -> Optional[asyncio.Task]:
        """Re-runs failed jobs for a given batch."""
        if not self.store.verify_recovery(run_id, recovery_code):
            raise PermissionError("Invalid recovery code for this run.")

        return self.start_or_resume_run(
            run_id=run_id,
            recovery_code=recovery_code,
            background=background,
        )

    def package_run_artifacts(self, run_id: str, recovery_code: str, *, persist=True) -> ArtifactResult:
        """
        Independently builds or retries packaging the batch ZIP bundle
        from already generated and verified PDFs.
        """
        if not self.store.verify_recovery(run_id, recovery_code):
            raise PermissionError("Invalid recovery code for this run.")

        run = self.store.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found.")

        return self._package(run, persist=persist)

    def _package(self, run, *, persist=True):
        run_id = run["run_id"]
        reviewed = bool(json.loads(run["config_json"]).get("reviewer_model"))
        completed_results = self.store.get_completed_job_results(run_id)
        res_dicts = [r.to_dict() for r in completed_results]

        profile_json = run.get("profile_json", "{}")
        profile = json.loads(profile_json)
        p_info = profile.get("personal_information", {})
        cand_name = f"{p_info.get('name', 'Candidate')} {p_info.get('surname', '')}".strip()

        pdf_contents = {} if reviewed else None
        if pdf_contents is not None:
            for result in res_dicts:
                content = approved_pdf_bytes(result)
                if content is not None:
                    pdf_contents[result["pdf_path"]] = content
                    continue
                result["pdf_path"] = result["pdf_filename"] = None
                if result["review_status"] == "Approved":
                    result["review_status"] = result["status"] = "Artifact verification failed"

        verified_pdf_paths = [
            r["pdf_path"] for r in res_dicts
            if r.get("pdf_path") and (r.get("audit_result") or {}).get("passed") is True
        ]
        summary_csv = generate_summary_csv(res_dicts)
        summary_html = render_summary_html(
            results=res_dicts, candidate_name=cand_name, threshold=run.get("score_threshold", 70),
            approval_required=reviewed,
        )
        zip_bytes = build_batch_zip_bytes(
            pdf_paths=verified_pdf_paths, summary_csv_str=summary_csv, summary_html_str=summary_html,
            pdf_contents=pdf_contents,
        )
        zip_path = self.store.get_run_artifacts_dir(run_id) / "jobhunt_tailored_batch.zip"
        if persist:
            temp = zip_path.with_name(f".{uuid.uuid4().hex}.zip.tmp")
            try:
                temp.write_bytes(zip_bytes)
                temp.replace(zip_path)
            finally:
                temp.unlink(missing_ok=True)
            self.store.record_artifact(run_id, "zip", str(zip_path), metadata={"verified_resumes": len(verified_pdf_paths)})

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

        if run["status"] == RunStatus.RUNNING.value and (run.get("lease_expires_at") or 0) <= time.time():
            self.store.recover_interrupted_runs()
            run = self.store.get_run(run_id) or run

        verified = False
        if recovery_code:
            verified = self.store.verify_recovery(run_id, recovery_code)

        events = self.store.get_stage_events(run_id, limit=20) if verified else []
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

    def _event(self, run_id, stage, status, message, job=None):
        self.store.record_stage_event(StageEvent(
            event_id=uuid.uuid4().hex[:12], run_id=run_id, stage=stage.value,
            status=status, message=message, job_id=job.job_id if job else None,
        ))

    async def _run_worker(self, run_id, worker_id, api_key, config: RunConfig):
        lease_lost = False

        async def heartbeat():
            nonlocal lease_lost
            while True:
                await asyncio.sleep(15)
                if not self.store.renew_lease(run_id, worker_id):
                    lease_lost = True
                    return

        def check_pause():
            if lease_lost or self._pause_flags.get(run_id):
                raise RunPaused()

        heartbeat_task = asyncio.create_task(heartbeat())
        client = None
        try:
            client = DeepSeekClient(api_key=api_key, model=MODEL, concurrency=CONCURRENCY)
            run = self.store.get_run(run_id)
            profile = json.loads(run["profile_json"])
            jobs = self.store.get_job_inputs(run_id)
            existing = {r.job_id: r for r in self.store.get_completed_job_results(run_id)}
            slots = asyncio.Semaphore(CONCURRENCY)

            async def score(job):
                async with slots:
                    check_pause()
                    result = existing.get(job.job_id)
                    if result and result.scoring_status == "success":
                        return result
                    self._event(run_id, Stage.SCORE, "start", f"Scoring {job.company} · {job.title}", job)
                    data = await score_job_async(
                        job=job.to_dict(), profile=profile, ai_client=client,
                    )
                    result = JobResult.from_dict(data)
                    self.store.save_job_result(run_id, result, Stage.SCORE)
                    if result.scoring_status == "failed":
                        self._event(run_id, Stage.SCORE, "failed", f"Scoring failed for {job.company}", job)
                    return result

            async def run_phase(coroutines):
                # Let in-flight stages checkpoint before leaving a paused or interrupted phase.
                outcomes = await asyncio.gather(*coroutines, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, BaseException):
                        raise outcome
                return outcomes

            results = await run_phase(score(job) for job in jobs)
            check_pause()
            selected = []
            for job, result in zip(jobs, results):
                if result.scoring_status != "success":
                    continue
                if result.match_score < config.score_threshold:
                    result.status = "Below Threshold"
                elif len(selected) >= config.max_resumes:
                    result.status = "Cap Reached"
                else:
                    selected.append((job, result))
                    continue
                self.store.save_job_result(run_id, result, Stage.SCORE)

            async def build(job, result):
                async with slots:
                    check_pause()
                    if approved_pdf(result.to_dict()) or result.review_status == "Needs manual review":
                        return
                    try:
                        await self._build_resume(run_id, config, profile, job, result, client, check_pause)
                    except (RunPaused, CredentialsRequired):
                        raise
                    except Exception as error:
                        result.status = "Resume Error"
                        result.error = str(error) if isinstance(error, RuntimeError) else "Resume processing failed."
                        self.store.save_job_result(run_id, result, Stage.CHECK)
                        self._event(run_id, Stage.CHECK, "failed", f"Resume processing failed for {job.company}", job)

            await run_phase(build(job, result) for job, result in selected)
            check_pause()
            self._event(run_id, Stage.PACKAGE, "start", "Building the results bundle")
            artifact = self._package(run)
            self.store.update_run_status(run_id, RunStatus.COMPLETED)
            self._event(run_id, Stage.PACKAGE, "success", f"Batch complete · {artifact.verified_count} resumes packaged")
        except RunPaused:
            if not lease_lost:
                self.store.update_run_status(run_id, RunStatus.PAUSED)
        except CredentialsRequired as error:
            self.store.update_run_status(run_id, RunStatus.AWAITING_CREDENTIALS)
            self._event(run_id, Stage.VALIDATE, "failed", str(error))
        except Exception:
            self.store.update_run_status(run_id, RunStatus.FAILED)
            self._event(run_id, Stage.VALIDATE, "failed", "Batch interrupted. Resume to continue from saved progress.")
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if client is not None:
                await client.close()
            self.store.release_lease(run_id, worker_id)
            self._active_tasks.pop(run_id, None)
            self._pause_flags.pop(run_id, None)

    async def review_job_resume_async(
        self,
        job: Any,
        profile: Dict[str, Any],
        client: DeepSeekClient,
        *,
        resumes_dir: Union[str, Path] = "resumes",
        config: Optional[RunConfig] = None,
        custom_filename: Optional[str] = None,
        on_draft_callback: Optional[Any] = None,
        precalculated_score: Optional[int] = None,
        company_context: Optional[Dict[str, Any]] = None,
    ) -> JobResult:
        """
        Public programmatic method to run the complete Challenger workflow on a single job.
        Reuses the exact Studio RunService implementation and approval rules.
        """
        if config is None:
            config = RunConfig(
                run_id="",
                provider=PROVIDER,
                model=MODEL,
                reviewer_model=REVIEW_MODEL,
                approval_threshold=85,
                max_drafts=3,
            )

        job_dict = job.to_dict() if hasattr(job, "to_dict") else dict(job)
        job_id = job_dict.get("job_id", f"job_{int(time.time()*1000)}")
        company = getattr(job, "company", None) or job_dict.get("company", "Unknown")
        title = getattr(job, "title", None) or job_dict.get("title", "Position")
        url = job_dict.get("url") or job_dict.get("direct_apply_link", "")
        location = job_dict.get("location", "")
        jd_text = job_dict.get("jd_text") or job_dict.get("exact_job_description", "")

        result = JobResult(
            job_id=job_id,
            company=company,
            title=title,
            url=url,
            location=location,
            jd_text=jd_text,
            match_score=precalculated_score if precalculated_score is not None else 0,
            scoring_status="success" if precalculated_score is not None else "pending",
            company_context=company_context,
        )

        await self._build_resume(
            run_id=None,
            config=config,
            profile=profile,
            job=job_dict,
            result=result,
            client=client,
            check_pause=lambda: None,
            resumes_dir=resumes_dir,
            custom_filename=custom_filename,
            on_draft_callback=on_draft_callback,
        )
        return result

    async def _build_resume(
        self,
        run_id: Optional[str],
        config: RunConfig,
        profile: Dict[str, Any],
        job: Any,
        result: JobResult,
        client: DeepSeekClient,
        check_pause: Optional[Any] = None,
        resumes_dir: Optional[Union[str, Path]] = None,
        custom_filename: Optional[str] = None,
        on_draft_callback: Optional[Any] = None,
    ):
        if check_pause is None:
            check_pause = lambda: None

        if resumes_dir is None:
            if run_id:
                resumes_dir = self.store.get_run_artifacts_dir(run_id) / "resumes"
            else:
                resumes_dir = Path("resumes")
        resumes_dir = Path(resumes_dir)
        resumes_dir.mkdir(parents=True, exist_ok=True)

        job_dict = job.to_dict() if hasattr(job, "to_dict") else dict(job)
        company = getattr(job, "company", None) or job_dict.get("company", "Unknown")
        title = getattr(job, "title", None) or job_dict.get("title", "Position")
        job_id = getattr(job, "job_id", None) or job_dict.get("job_id", "")

        candidate = profile.get("personal_information", {})
        cand_name_str = f"{candidate.get('name', '')}_{candidate.get('surname', '')}".strip("_")
        base_filename = custom_filename or build_safe_pdf_filename(
            company, title, job_id, cand_name_str,
        )

        async def render(resume, filename):
            check_pause()
            if run_id:
                self._event(run_id, Stage.RENDER, "start", f"Rendering PDF for {company}", job)
            return await asyncio.to_thread(
                render_and_audit_pdf, tailored_resume=resume, output_dir=resumes_dir,
                job=job_dict, custom_filename=filename,
            )

        if result.company_context is None:
            if run_id:
                self._event(run_id, Stage.RESEARCH, "start", f"Checking sources for {company}", job)
            result.company_context = await asyncio.to_thread(collect_company_context, job_dict)
            if run_id:
                self.store.save_job_result(run_id, result, Stage.RESEARCH)

        if result.review_status == "Approved" and result.attempts:
            result.attempts[-1]["review"] = None
            result.review_status, result.status = "Unreviewed", "Draft generated"
            result.pdf_path = result.pdf_filename = None
            if run_id:
                self.store.save_job_result(run_id, result, Stage.CHALLENGE)

        while True:
            check_pause()
            prior = result.attempts[-1] if result.attempts else None
            if prior is None or prior.get("review") is not None:
                if prior and (prior.get("unchanged") or len(result.attempts) >= config.max_drafts or any(
                    f["severity"] in ("unsupported_requirement", "source_conflict") for f in prior["review"]["findings"]
                )):
                    break
                stage = Stage.REVISE if prior else Stage.TAILOR
                if run_id:
                    self._event(run_id, stage, "start", f"{stage.value} draft {len(result.attempts) + 1} for {company}", job)
                usage = {}
                resume = await tailor_resume_async(
                    job_dict, profile, ai_client=client, company_context=result.company_context,
                    previous_resume=prior["resume"] if prior else None,
                    review_feedback={"review": prior["review"], "audit": prior["audit"]} if prior else None,
                    usage=usage,
                )
                attempt = asdict(ResumeAttempt(len(result.attempts) + 1, resume, model=config.model, **usage))
                result.attempts.append(attempt)
                result.prompt_tokens += attempt["prompt_tokens"]
                result.completion_tokens += attempt["completion_tokens"]
                result.status, result.error = "Draft generated", None
                if prior and resume == prior["resume"]:
                    attempt.update(unchanged=True, review={**prior["review"], "approved": False},
                                   audit=prior["audit"], pdf_path=prior["pdf_path"], pdf_hash=prior["pdf_hash"])
                if run_id:
                    self.store.save_job_result(run_id, result, stage)
                if attempt.get("unchanged"):
                    break
            else:
                attempt = prior

            path = attempt["pdf_path"]
            if not path or not Path(path).is_file() or pdf_digest(path) != attempt["pdf_hash"]:
                path, audit = await render(attempt["resume"], f"draft-{attempt['number']}_{base_filename}")
                attempt.update(pdf_path=path, pdf_hash=pdf_digest(path), audit=audit)
                result.draft_path = path
                if run_id:
                    self.store.save_job_result(run_id, result, Stage.RENDER)

            check_pause()
            if run_id:
                self._event(run_id, Stage.CHALLENGE, "start", f"Reviewing draft {attempt['number']} for {company}", job)
            while not _review_slots.acquire(blocking=False):
                await asyncio.sleep(0.1)
                check_pause()
            try:
                audit, review = await review_resume_async(
                    job_dict, profile, result.company_context, attempt, client, config.approval_threshold,
                )
            finally:
                _review_slots.release()
            if pdf_digest(path) != attempt["pdf_hash"]:
                raise RuntimeError("Draft changed during review. Retry the draft review.")
            attempt.update(audit=audit, review=review, reviewer_model=config.reviewer_model)
            result.challenger_score = review["score"]
            result.prompt_tokens += review["prompt_tokens"]
            result.completion_tokens += review["completion_tokens"]
            result.audit_result = audit
            result.error = None
            result.status = "Reviewed draft"
            if review["approved"]:
                result.status = result.review_status = "Approved"
                result.pdf_path, result.pdf_filename = path, Path(path).name
                result.tailored_resume = attempt["resume"]
            if run_id:
                self.store.save_job_result(run_id, result, Stage.CHALLENGE)

            if on_draft_callback:
                try:
                    on_draft_callback(
                        draft_number=attempt["number"],
                        score=review["score"],
                        status=result.status,
                        approved=review["approved"],
                        findings=review.get("findings", []),
                        audit=audit,
                        pdf_path=path,
                    )
                except Exception as cb_err:
                    logger.warning(f"on_draft_callback error: {cb_err}")

            if review["approved"]:
                if run_id:
                    self._event(run_id, Stage.CHALLENGE, "success", f"Approved {company} · {review['score']:.1f}/100", job)
                return

        best = max(result.attempts, key=lambda a: (a.get("review") or {}).get("score", -1))
        result.status = result.review_status = "Needs manual review"
        result.draft_path = best["pdf_path"]
        result.pdf_path = result.pdf_filename = None
        result.challenger_score = best["review"]["score"]
        result.tailored_resume, result.audit_result = best["resume"], best["audit"]
        if result.attempts[-1].get("unchanged"):
            result.error = "DeepSeek returned the same draft. Review the unresolved findings before proceeding."
        if run_id:
            self.store.save_job_result(run_id, result, Stage.CHALLENGE)


run_service_instance = RunService()
