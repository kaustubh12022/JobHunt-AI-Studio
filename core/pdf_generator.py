"""
JobHunt AI Studio - Playwright Single-Page PDF Generator
Renders ATS-compliant single-page resumes via headless Chromium with strict 1-page bounds.
Includes:
- Strict Chromium concurrency limit of 1
- Disabling external network requests and script execution
- Safe collision-free filenames incorporating stable job IDs
- Jinja2 autoescaping
- Automated ATS audit gate returning structured diagnostics
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from core.ats_verifier import audit_resume_pdf
from core.security import build_safe_pdf_filename

logger = logging.getLogger("JobHuntAIStudio")

# Concurrency constraint: strictly 1 Chromium render at a time to prevent resource contention
_chromium_semaphore = threading.Semaphore(1)
_pdf_executor = ThreadPoolExecutor(max_workers=2)


def render_html(tailored_resume: Dict[str, Any], job: Optional[Dict[str, Any]] = None, compact: bool = False) -> str:
    """Renders HTML template with Jinja2 using strict autoescaping."""
    template_dir = Path(__file__).parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("resume_template.html")
    return template.render(
        resume=tailored_resume,
        job=job or {},
        compact_mode=compact,
    )


def _render_pdf_sync(
    tailored_resume: Dict[str, Any],
    output_dir: Union[str, Path],
    job: Optional[Dict[str, Any]] = None,
    custom_filename: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Renders single page PDF in isolated thread with semaphore concurrency = 1.
    Blocks external network requests and returns (pdf_path, audit_dict).
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    p_info = tailored_resume.get("personal_information", {})
    c_name = f"{p_info.get('name', 'Candidate')}_{p_info.get('surname', '')}".strip('_') or "Candidate"
    company = (job.get("company") if job else "") or "Company"
    title = (job.get("title") if job else "") or "Software_Engineer"
    job_id = (job.get("job_id") if job else "") or "doc"

    filename = custom_filename or build_safe_pdf_filename(company, title, job_id, c_name)
    pdf_path = out_dir / filename

    html_content = render_html(tailored_resume, job, compact=False)

    with _chromium_semaphore:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = browser.new_context(
                java_script_enabled=False,
                bypass_csp=False,
            )
            page = context.new_page()

            # Security: block external network requests (prevent SSRF/outbound network requests)
            def _block_external(route):
                url = route.request.url.lower()
                if url.startswith("http://") or url.startswith("https://"):
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", _block_external)

            cand_loc = p_info.get("address") or p_info.get("city") or p_info.get("location")

            try:
                page.set_content(html_content, wait_until="load")
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "0in", "right": "0in", "bottom": "0in", "left": "0in"},
                )

                audit = audit_resume_pdf(str(pdf_path), candidate_location=cand_loc)
                if not audit["passed"] and any("PAGE COUNT VIOLATION" in err for err in audit["errors"]):
                    logger.warning(f"PDF {filename} exceeded 1 page ({audit['details'].get('page_count')} pages). Re-rendering in compact mode...")
                    compact_html = render_html(tailored_resume, job, compact=True)
                    page.set_content(compact_html, wait_until="load")
                    page.pdf(
                        path=str(pdf_path),
                        format="A4",
                        print_background=True,
                        margin={"top": "0in", "right": "0in", "bottom": "0in", "left": "0in"},
                    )
                    audit = audit_resume_pdf(str(pdf_path), candidate_location=cand_loc)

                if audit["passed"]:
                    logger.info(f"ATS Audit Verified [1-Page ATS Safe]: {filename}")
                else:
                    logger.warning(f"ATS Audit flags in {filename}: {audit['errors']}")

                return str(pdf_path), audit

            finally:
                context.close()
                browser.close()


def generate_single_page_pdf(
    tailored_resume: Dict[str, Any],
    output_dir: Union[str, Path],
    job: Optional[Dict[str, Any]] = None,
    custom_filename: Optional[str] = None,
) -> str:
    """
    Backward-compatible entry point returning str(pdf_path).
    Safe to invoke inside or outside an active asyncio loop.
    """
    path, _ = render_and_audit_pdf(tailored_resume, output_dir, job, custom_filename)
    return path


def render_and_audit_pdf(
    tailored_resume: Dict[str, Any],
    output_dir: Union[str, Path],
    job: Optional[Dict[str, Any]] = None,
    custom_filename: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Renders PDF and performs universal ATS audit.
    Returns (pdf_path, structured_audit_results).
    """
    try:
        asyncio.get_running_loop()
        future = _pdf_executor.submit(
            _render_pdf_sync, tailored_resume, output_dir, job, custom_filename
        )
        return future.result()
    except RuntimeError:
        return _render_pdf_sync(tailored_resume, output_dir, job, custom_filename)
