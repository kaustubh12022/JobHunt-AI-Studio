"""
JobHunt AI Studio - Headless Background Batch Runner
Executes two-phase batch processing (Score All -> Tailor Qualified -> Package)
inside an isolated server-side daemon thread that survives client browser disconnections,
tab sleeps, and device power-offs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.ai_engine import ScopedAIClient
import core.ats_verifier as ats_verifier
from core.models import ArtifactResult
from core.packager import build_batch_zip_bytes
import core.pdf_generator as pdf_generator
import core.resume_tailor as resume_tailor
import core.scorer as scorer
from core.session_manager import session_manager
from core.summary_reporter import generate_summary_csv, render_summary_html

logger = logging.getLogger("JobHuntAIStudio")


def run_coro_sync(coro):
    """Safely runs an async coroutine synchronously inside the worker thread."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        return loop.run_until_complete(coro)
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()


class BatchRunner:
    """
    Manages background batch runs per session.
    Persists progress to disk so runs survive browser reloads and tab closures.
    """

    def __init__(self):
        self._threads: Dict[str, threading.Thread] = {}
        self._stop_flags: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def get_state_file(self, session_id: str) -> Path:
        return session_manager.get_session_dir(session_id) / "run_state.json"

    def get_run_state(self, session_id: str) -> Dict[str, Any]:
        """Reads current execution state from disk."""
        state_file = self.get_state_file(session_id)
        if not state_file.exists():
            return {"status": "idle"}
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error reading run_state for {session_id}: {e}")
            return {"status": "idle"}

    def _save_state(self, session_id: str, state_data: Dict[str, Any]):
        """Persists execution state atomically to disk."""
        state_file = self.get_state_file(session_id)
        tmp_file = state_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2)
            tmp_file.replace(state_file)
        except Exception as e:
            logger.warning(f"Error saving run_state for {session_id}: {e}")

    def is_running(self, session_id: str) -> bool:
        """Returns True if a background thread is actively processing for session_id."""
        with self._lock:
            thread = self._threads.get(session_id)
            if thread and thread.is_alive():
                return True
        state = self.get_run_state(session_id)
        return state.get("status") == "running"

    def stop_batch(self, session_id: str):
        """Signals the background worker to halt execution."""
        self._stop_flags[session_id] = True
        state = self.get_run_state(session_id)
        if state.get("status") == "running":
            state["status"] = "paused"
            state["logs"] = state.get("logs", []) + ["Batch paused by user request."]
            self._save_state(session_id, state)

    def clear_batch(self, session_id: str):
        """Stops any active run and purges state files."""
        self.stop_batch(session_id)
        state_file = self.get_state_file(session_id)
        if state_file.exists():
            try:
                state_file.unlink()
            except Exception:
                pass
        session_manager.clear_checkpoint(session_id)

    def start_batch(
        self,
        session_id: str,
        jobs: List[Dict[str, Any]],
        profile: Dict[str, Any],
        provider: str,
        api_keys: List[str],
        model: str,
        score_threshold: int = 70,
        max_resumes: int = 20,
        is_gemini_free: bool = True,
        gemini_rpm: int = 15,
        resume_from_checkpoint: bool = False,
        wait_until_complete: bool = False,
        on_pipeline_update: Optional[Callable[[str, str, str, bool, bool], None]] = None,
    ):
        """Spawns a decoupled background worker thread."""
        with self._lock:
            if session_id in self._threads and self._threads[session_id].is_alive():
                logger.warning(f"Batch already active for session {session_id}")
                if wait_until_complete:
                    self._threads[session_id].join()
                return

            self._stop_flags[session_id] = False
            thread = threading.Thread(
                target=self._worker,
                kwargs={
                    "session_id": session_id,
                    "jobs": jobs,
                    "profile": profile,
                    "provider": provider,
                    "api_keys": api_keys,
                    "model": model,
                    "score_threshold": score_threshold,
                    "max_resumes": max_resumes,
                    "is_gemini_free": is_gemini_free,
                    "gemini_rpm": gemini_rpm,
                    "resume_from_checkpoint": resume_from_checkpoint,
                    "on_pipeline_update": on_pipeline_update,
                },
                daemon=True,
                name=f"batch_worker_{session_id}",
            )
            self._threads[session_id] = thread
            thread.start()

            if wait_until_complete:
                thread.join()

    def _worker(
        self,
        session_id: str,
        jobs: List[Dict[str, Any]],
        profile: Dict[str, Any],
        provider: str,
        api_keys: List[str],
        model: str,
        score_threshold: int,
        max_resumes: int,
        is_gemini_free: bool,
        gemini_rpm: int,
        resume_from_checkpoint: bool,
        on_pipeline_update: Optional[Callable[[str, str, str, bool, bool], None]] = None,
    ):
        """
        Background worker thread executing two-phase batch processing:
        Phase 1: Score all jobs sequentially/with key rotation
        Phase 2: Tailor & render PDFs for qualified jobs
        Phase 3: Package summary reports and ZIP
        """
        logger.info(f"Background batch started for session {session_id} with {len(jobs)} jobs.")
        rpm = 15 if is_gemini_free else gemini_rpm

        # 1. Initialize run-scoped client
        try:
            ai_client = ScopedAIClient(
                provider=provider,
                api_keys=api_keys,
                model=model,
                rpm_limit=rpm,
            )
        except Exception as e:
            logger.error(f"Failed to initialize ScopedAIClient for session {session_id}: {e}")
            self._save_state(session_id, {
                "status": "failed",
                "error": f"Client initialization failed: {e}",
                "phase": "init",
                "completed_count": 0,
                "total_jobs": len(jobs),
                "logs": [f"Fatal initialization error: {e}"],
            })
            return

        resumes_dir = session_manager.get_resumes_dir(session_id)
        total_jobs = len(jobs)
        results_map: Dict[int, Dict[str, Any]] = {}
        logs: List[str] = []

        # Check existing checkpoint
        if resume_from_checkpoint:
            cp = session_manager.load_checkpoint(session_id)
            if cp and cp.get("completed_jobs"):
                for idx, r in enumerate(cp["completed_jobs"]):
                    results_map[idx] = r
                logs.append(f"Resumed from checkpoint: {len(results_map)} jobs already processed.")

        start_time = time.time()
        initial_state = {
            "status": "running",
            "phase": "scoring",
            "completed_count": len(results_map),
            "total_jobs": total_jobs,
            "qualified_count": sum(1 for r in results_map.values() if r.get("match_score", 0) >= score_threshold),
            "tailored_count": sum(1 for r in results_map.values() if r.get("pdf_path")),
            "logs": logs,
            "start_time": start_time,
            "current_job": None,
        }
        self._save_state(session_id, initial_state)

        try:
            # ─────────────────────────────────────────────────────────────
            # PHASE 1: SCORE ALL JOBS
            # ─────────────────────────────────────────────────────────────
            logs.append(f"Phase 1: Scoring all {total_jobs} jobs...")
            for i in range(total_jobs):
                if self._stop_flags.get(session_id, False):
                    logs.append("Phase 1 stopped by user.")
                    self._save_state(session_id, {
                        "status": "paused",
                        "phase": "scoring",
                        "completed_count": len(results_map),
                        "total_jobs": total_jobs,
                        "results": list(results_map.values()),
                        "logs": logs,
                    })
                    return

                # If job was already scored, reuse
                if i in results_map and results_map[i].get("scoring_status") == "success":
                    continue

                job = jobs[i]
                log_msg = f"Scoring {i + 1}/{total_jobs}: {job.get('company', 'Unknown')} — {job.get('title', 'Role')}"
                logs.append(log_msg)
                if len(logs) > 100:
                    logs = logs[-100:]

                current_job_info = {
                    "company": job.get("company", "Unknown"),
                    "title": job.get("title", "Role"),
                    "index": i + 1,
                    "stage": "Scoring",
                }
                if on_pipeline_update:
                    try:
                        on_pipeline_update("Scoring", job.get("company", "Unknown"), job.get("title", "Role"), False, False)
                    except Exception:
                        pass
                self._save_state(session_id, {
                    "status": "running",
                    "phase": "scoring",
                    "completed_count": len(results_map),
                    "total_jobs": total_jobs,
                    "current_job": current_job_info,
                    "logs": logs,
                    "start_time": start_time,
                })

                # Score single job asynchronously
                scored = run_coro_sync(
                    scorer.score_job_async(
                        job=job,
                        profile=profile,
                        model=model,
                        provider=provider,
                        api_keys=api_keys,
                        ai_client=ai_client,
                    )
                )

                score = scored.get("match_score", 0)
                scored["status"] = "Evaluated"
                results_map[i] = scored

                # Save checkpoint after each scored job
                session_manager.save_checkpoint(
                    session_id=session_id,
                    total_jobs=total_jobs,
                    completed_jobs=list(results_map.values()),
                    threshold=score_threshold,
                    model=model,
                    provider=provider,
                )

                self._save_state(session_id, {
                    "status": "running",
                    "phase": "scoring",
                    "completed_count": len(results_map),
                    "total_jobs": total_jobs,
                    "current_job": current_job_info,
                    "qualified_count": sum(1 for r in results_map.values() if r.get("match_score", 0) >= score_threshold),
                    "tailored_count": 0,
                    "results": list(results_map.values()),
                    "logs": logs,
                    "start_time": start_time,
                })

            # ─────────────────────────────────────────────────────────────
            # PHASE 2: TAILOR & GENERATE PDFS FOR QUALIFIED JOBS
            # ─────────────────────────────────────────────────────────────
            # Identify qualified jobs meeting threshold
            qualified_indices = [
                i for i in range(total_jobs)
                if results_map.get(i, {}).get("match_score", 0) >= score_threshold
            ]
            target_indices = qualified_indices[:max_resumes]
            logs.append(
                f"Phase 1 complete! {len(qualified_indices)} jobs met threshold (>= {score_threshold}%). "
                f"Proceeding to tailor top {len(target_indices)} resumes..."
            )

            for t_idx, idx in enumerate(target_indices):
                if self._stop_flags.get(session_id, False):
                    logs.append("Phase 2 stopped by user.")
                    self._save_state(session_id, {
                        "status": "paused",
                        "phase": "tailoring",
                        "completed_count": len(results_map),
                        "total_jobs": total_jobs,
                        "results": list(results_map.values()),
                        "logs": logs,
                    })
                    return

                scored_job = results_map[idx]
                if scored_job.get("pdf_path") and os.path.exists(scored_job["pdf_path"]):
                    continue

                log_msg = f"Tailoring {t_idx + 1}/{len(target_indices)}: {scored_job.get('company')} (Score: {scored_job.get('match_score')}%)"
                logs.append(log_msg)
                if len(logs) > 100:
                    logs = logs[-100:]

                current_job_info = {
                    "company": scored_job.get("company", "Unknown"),
                    "title": scored_job.get("title", "Role"),
                    "index": t_idx + 1,
                    "total_tailor": len(target_indices),
                    "stage": "Tailoring",
                }
                if on_pipeline_update:
                    try:
                        on_pipeline_update("Tailoring", scored_job.get("company", "Unknown"), scored_job.get("title", "Role"), False, False)
                    except Exception:
                        pass
                self._save_state(session_id, {
                    "status": "running",
                    "phase": "tailoring",
                    "completed_count": total_jobs,
                    "total_jobs": total_jobs,
                    "current_job": current_job_info,
                    "qualified_count": len(qualified_indices),
                    "tailoring_current": t_idx + 1,
                    "tailoring_total": len(target_indices),
                    "tailored_count": sum(1 for r in results_map.values() if r.get("pdf_path")),
                    "results": list(results_map.values()),
                    "logs": logs,
                    "start_time": start_time,
                })

                # Tailor resume
                tailored_resume = run_coro_sync(
                    resume_tailor.tailor_resume_async(
                        job=scored_job,
                        profile=profile,
                        model=model,
                        provider=provider,
                        api_keys=api_keys,
                        ai_client=ai_client,
                    )
                )
                scored_job["tailored_resume"] = tailored_resume

                # Generate single-page PDF
                current_job_info["stage"] = "Rendering PDF"
                if on_pipeline_update:
                    try:
                        on_pipeline_update("PDF generation", scored_job.get("company", "Unknown"), scored_job.get("title", "Role"), False, False)
                    except Exception:
                        pass
                try:
                    pdf_path = pdf_generator.generate_single_page_pdf(
                        tailored_resume=tailored_resume,
                        output_dir=resumes_dir,
                        job=scored_job,
                    )
                    scored_job["pdf_path"] = pdf_path
                    scored_job["pdf_filename"] = os.path.basename(pdf_path)
                    scored_job["status"] = "Tailored & Verified"

                    # Audit PDF
                    audit_res = ats_verifier.audit_resume_pdf(pdf_path)
                    scored_job["audit_result"] = audit_res
                    logs.append(f"PDF generated & verified: {os.path.basename(pdf_path)}")
                except Exception as exc:
                    logger.error(f"PDF generation failed for {scored_job.get('company')}: {exc}")
                    scored_job["pdf_path"] = None
                    scored_job["pdf_filename"] = None
                    scored_job["status"] = f"Render Error: {str(exc)[:30]}"
                    logs.append(f"PDF generation failed for {scored_job.get('company')}: {exc}")

                results_map[idx] = scored_job

                # Save checkpoint after each PDF
                session_manager.save_checkpoint(
                    session_id=session_id,
                    total_jobs=total_jobs,
                    completed_jobs=list(results_map.values()),
                    threshold=score_threshold,
                    model=model,
                    provider=provider,
                )
                self._save_state(session_id, {
                    "status": "running",
                    "phase": "tailoring",
                    "completed_count": total_jobs,
                    "total_jobs": total_jobs,
                    "current_job": current_job_info,
                    "qualified_count": len(qualified_indices),
                    "tailoring_current": t_idx + 1,
                    "tailoring_total": len(target_indices),
                    "tailored_count": sum(1 for r in results_map.values() if r.get("pdf_path")),
                    "results": list(results_map.values()),
                    "logs": logs,
                    "start_time": start_time,
                })

            # Mark statuses for below-threshold or cap-reached jobs
            for i in range(total_jobs):
                if i not in target_indices and i in results_map:
                    sc = results_map[i].get("match_score", 0)
                    if sc < score_threshold:
                        results_map[i]["status"] = "Below Threshold"
                    else:
                        results_map[i]["status"] = "Cap Reached"
                    results_map[i]["pdf_path"] = None
                    results_map[i]["pdf_filename"] = None

            # ─────────────────────────────────────────────────────────────
            # PHASE 3: PACKAGING & SUMMARY
            # ─────────────────────────────────────────────────────────────
            logs.append("Phase 3: Packaging summary reports and tailored resumes...")
            if on_pipeline_update:
                try:
                    on_pipeline_update("Packaging", "Preparing your downloads", "Building bundle", False, False)
                except Exception:
                    pass
            self._save_state(session_id, {
                "status": "running",
                "phase": "packaging",
                "completed_count": total_jobs,
                "total_jobs": total_jobs,
                "logs": logs,
                "start_time": start_time,
            })

            all_results = [results_map[i] for i in range(total_jobs) if i in results_map]
            p_info = profile.get("personal_information", {})
            cand_name = f"{p_info.get('name', 'Candidate')} {p_info.get('surname', '')}".strip()

            summary_csv_str = generate_summary_csv(all_results)
            summary_html_str = render_summary_html(
                results=all_results,
                candidate_name=cand_name,
                threshold=score_threshold,
            )

            verified_pdf_paths = [
                r["pdf_path"] for r in all_results
                if r.get("pdf_path") and os.path.exists(r["pdf_path"])
            ]

            zip_bytes = build_batch_zip_bytes(
                pdf_paths=verified_pdf_paths,
                summary_csv_str=summary_csv_str,
                summary_html_str=summary_html_str,
            )

            zip_path = session_manager.get_session_dir(session_id) / "jobhunt_tailored_batch.zip"
            with open(zip_path, "wb") as f:
                f.write(zip_bytes)

            csv_path = session_manager.get_session_dir(session_id) / "batch_summary.csv"
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write(summary_csv_str)

            elapsed_total = int(time.time() - start_time)
            logs.append(
                f"Batch completed in {elapsed_total}s! "
                f"{len(all_results)} evaluated, {len(verified_pdf_paths)} resumes tailored & packaged."
            )

            final_state = {
                "status": "completed",
                "phase": "completed",
                "completed_count": len(all_results),
                "total_jobs": total_jobs,
                "qualified_count": len(qualified_indices),
                "tailored_count": len(verified_pdf_paths),
                "results": all_results,
                "summary_csv": summary_csv_str,
                "zip_path": str(zip_path),
                "logs": logs,
                "elapsed_seconds": elapsed_total,
            }
            self._save_state(session_id, final_state)
            session_manager.clear_checkpoint(session_id)
            if on_pipeline_update:
                try:
                    on_pipeline_update(None, "Your batch is ready", "Downloads ready", True, False)
                except Exception:
                    pass
            logger.info(f"Background batch finished successfully for session {session_id}.")

        except Exception as exc:
            logger.error(f"Fatal error in background worker for {session_id}: {exc}", exc_info=True)
            logs.append(f"Batch interrupted: {exc}")
            if on_pipeline_update:
                try:
                    on_pipeline_update(None, "Batch interrupted", str(exc), False, True)
                except Exception:
                    pass
            self._save_state(session_id, {
                "status": "failed",
                "phase": "error",
                "error": str(exc),
                "completed_count": len(results_map),
                "total_jobs": total_jobs,
                "results": list(results_map.values()),
                "logs": logs,
            })


batch_runner = BatchRunner()
