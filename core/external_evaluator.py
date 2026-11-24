"""
JobHunt AI Studio - Headless Owner Evaluator
Programmatic access to the locked studio pipeline (DeepSeek Flash) for scripts and
automation. This does not modify or bypass the web studio's owner gate.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from core.ai_engine import DeepSeekClient
from core.pdf_generator import render_and_audit_pdf
from core.profile_manager import load_profile_from_file
from core.resume_tailor import tailor_resume_async
from core.scorer import score_job_async
from core.settings import PROFILE_PATH, SCORE_THRESHOLD


def _run(coro):
    """Run a coroutine from synchronous code, including inside an active loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ExternalJobEvaluator:
    """Headless batch evaluator and resume generator using the studio's engine."""

    def __init__(
        self,
        profile_path: Optional[Union[str, Path]] = None,
        threshold: int = SCORE_THRESHOLD,
        client: Optional[DeepSeekClient] = None,
    ):
        self.profile_path = Path(profile_path) if profile_path else PROFILE_PATH
        self.profile = load_profile_from_file(self.profile_path)
        self.threshold = threshold
        self.client = client or DeepSeekClient()

    async def score_single_job_async(self, job_dict: Dict[str, Any]) -> Dict[str, Any]:
        return await score_job_async(job_dict, self.profile, ai_client=self.client)

    def score_single_job_sync(self, job_dict: Dict[str, Any]) -> Dict[str, Any]:
        return _run(self.score_single_job_async(job_dict))

    async def tailor_and_render_resume_async(
        self,
        job_dict: Dict[str, Any],
        output_dir: Union[str, Path],
        custom_filename: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        tailored = await tailor_resume_async(job_dict, self.profile, ai_client=self.client)
        return render_and_audit_pdf(tailored, output_dir, job_dict, custom_filename)

    def tailor_and_render_resume_sync(
        self,
        job_dict: Dict[str, Any],
        output_dir: Union[str, Path],
        custom_filename: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        return _run(self.tailor_and_render_resume_async(job_dict, output_dir, custom_filename))

    async def review_and_tailor_resume_async(
        self,
        job_dict: Dict[str, Any],
        output_dir: Union[str, Path],
        custom_filename: Optional[str] = None,
        on_draft_callback: Optional[Any] = None,
        precalculated_score: Optional[int] = None,
    ):
        """
        Run the full owner workflow:
        DeepSeek draft -> render PDF & audit -> DeepSeek Flash review -> revision loop.
        Approves at >= 85/100 when every required check passes.
        """
        from core.run_service import run_service_instance

        return await run_service_instance.review_job_resume_async(
            job=job_dict,
            profile=self.profile,
            client=self.client,
            resumes_dir=output_dir,
            custom_filename=custom_filename,
            on_draft_callback=on_draft_callback,
            precalculated_score=precalculated_score,
        )

    def review_and_tailor_resume_sync(
        self,
        job_dict: Dict[str, Any],
        output_dir: Union[str, Path],
        custom_filename: Optional[str] = None,
        on_draft_callback: Optional[Any] = None,
        precalculated_score: Optional[int] = None,
    ):
        return _run(self.review_and_tailor_resume_async(
            job_dict, output_dir, custom_filename, on_draft_callback, precalculated_score,
        ))
