"""JobHunt AI Studio: Kaustubh Kale's private resume desk.

The interface is deliberately reduced to one linear flow so a human or an
automated browser agent (Playwright / vision) can drive it without guessing:

    locked  ->  ready (upload a CSV)  ->  running  ->  complete

Every unlocked screen has exactly one ``<h1>``, one primary action, stable
widget labels and keys, a visible ``STATUS`` word, and a machine-readable
status banner (``data-studio-state`` and friends).
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

from core.access import access_configured, verify_access
from core.ats_verifier import audit_resume_pdf
from core.batch_runner import batch_runner
from core.cost_estimator import estimate_batch_cost
from core.csv_parser import generate_sample_csv_str, parse_jobs_csv
from core.models import JobInput, RunConfig, RunStatus
from core.profile_manager import load_owner_profile, profile_to_yaml_str
from core.resume_review import approved_pdf, approved_pdf_bytes
from core.run_service import run_service_instance as run_service
from core.run_store import run_store_instance
from core.security import hash_recovery_code, is_safe_url
from core.session_manager import session_manager
from core.settings import (
    APPROVAL_THRESHOLD,
    MAX_DRAFTS,
    MAX_RESUMES,
    MODEL,
    MODEL_LABEL,
    OWNER_EMAIL,
    OWNER_NAME,
    PROVIDER,
    REVIEW_MODEL,
    SCORE_THRESHOLD,
    deepseek_api_key,
)
from ui import (
    counts,
    display_status,
    render_live_log_console,
    status_banner,
    styles,
    table_html,
)

logger = logging.getLogger("JobHuntAIStudio")

st.set_page_config(
    page_title="JobHunt AI Studio",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="collapsed",
)
styles()


# ==========================================
# OWNER GATE (fails closed; nothing private renders before unlock)
# ==========================================
if not access_configured():
    st.title("Studio locked")
    st.error(
        "STUDIO_ACCESS_KEY is not configured on this server. "
        "Set a passphrase of at least 12 characters and reload the studio."
    )
    st.stop()

if not st.session_state.get("owner_unlocked", False):
    st.markdown(
        '<div class="topbar"><strong>JobHunt AI Studio</strong>'
        "<span>Private owner console</span></div>",
        unsafe_allow_html=True,
    )
    st.title("Owner access only")
    st.caption("This is a private studio. Enter the owner passphrase to continue.")
    passphrase = st.text_input("Owner passphrase", type="password", key="owner_passphrase")
    failed_attempts = st.session_state.get("owner_failed_attempts", 0)
    if st.button("Unlock studio", type="primary", use_container_width=True, key="unlock_studio"):
        if verify_access(passphrase):
            st.session_state["owner_unlocked"] = True
            st.session_state["owner_failed_attempts"] = 0
            st.rerun()
        st.session_state["owner_failed_attempts"] = failed_attempts + 1
        time.sleep(min(2.0, 0.25 * (failed_attempts + 1)))
        st.error("Incorrect passphrase.")
    if failed_attempts >= 3:
        st.caption("Repeated failures are rate-limited.")
    st.stop()

# ==========================================
# LOCKED SESSION SETUP
# ==========================================
st.session_state.setdefault("session_id", uuid.uuid4().hex[:12])
session_id = st.session_state.session_id

try:
    session_manager.cleanup_expired_sessions()
except Exception:  # pragma: no cover - housekeeping must never block the studio
    pass

# The profile always comes from the bundled owner file; it cannot be edited here.
st.session_state.profile = load_owner_profile()
st.session_state.setdefault("batch_results", None)
st.session_state.setdefault("zip_bytes", None)

run_id = st.session_state.get("current_run_id")
recovery_code = st.session_state.get("current_recovery_code")
active_run_state = run_service.get_run_status(run_id, recovery_code) if run_id else {}
authorized = bool(active_run_state.get("is_authorized", False))
run_config = active_run_state.get("config", {})
review_required = bool(run_config.get("reviewer_model", True))
events = active_run_state.get("events", [])
run_status = str(active_run_state.get("status", ""))
is_running = bool(
    run_id
    and authorized
    and (batch_runner.is_running(run_id) or run_status == RunStatus.RUNNING.value)
)
server_key_ready = bool(deepseek_api_key())


def clear_results() -> None:
    """Resets results, artifacts and run references for this session."""
    for key in (
        "batch_results",
        "zip_bytes",
        "summary_csv",
        "summary_html",
        "results_run_id",
        "pdf_checks",
        "current_run_id",
        "current_recovery_code",
        "inspected_run",
        "inspected_code",
        "inspect_job",
        "start_error",
    ):
        st.session_state.pop(key, None)


# Rebuild results, downloads and the export bundle once per finished run.
if authorized and not is_running and run_status in ("completed", "failed"):
    if st.session_state.get("results_run_id") != run_id:
        st.session_state.batch_results = active_run_state.get("completed_jobs") or None
        if st.session_state.batch_results:
            for result in st.session_state.batch_results:
                if review_required and result["review_status"] == "Approved" and not approved_pdf(result):
                    result["status"] = result["review_status"] = "Artifact verification failed"
                    result["pdf_path"] = None
            try:
                artifact = run_service.package_run_artifacts(run_id, recovery_code, persist=False)
            except Exception as error:  # pragma: no cover - packaging recovery path
                logger.exception("Could not build the export bundle")
                st.session_state["package_error"] = str(error)
            else:
                st.session_state.zip_bytes = artifact.zip_bytes
                st.session_state.summary_csv = artifact.summary_csv_str
                st.session_state.summary_html = artifact.summary_html_str
                st.session_state.pop("package_error", None)
        st.session_state["results_run_id"] = run_id

st.markdown(
    '<div class="topbar">'
    "<strong>JobHunt AI Studio</strong>"
    f"<span>Private owner console · {OWNER_NAME}</span>"
    "</div>",
    unsafe_allow_html=True,
)

screen = "ready"
if run_id and authorized and run_status in ("running", "paused", "awaiting_credentials", "pending"):
    screen = "running"
elif run_id and authorized and run_status in ("completed", "failed"):
    screen = "complete"


# ==========================================
# SCREEN 1: READY - upload a jobs CSV
# ==========================================
if screen == "ready":
    st.title("Upload a jobs CSV")
    st.caption(
        "Required columns: company, job title, description. "
        "The studio scores every job, tailors resumes for strong matches and reviews each PDF."
    )
    st.caption(
        f"Engine: {MODEL_LABEL} ({MODEL}) · match threshold {SCORE_THRESHOLD}% · "
        f"up to {MAX_RESUMES} resumes · all settings locked"
    )

    banner_slot = st.empty()

    uploaded_file = st.file_uploader(
        "Jobs CSV",
        type=["csv", "tsv", "txt"],
        key="jobs_csv",
        help="Company, job title and a description of at least 50 characters.",
    )
    with st.expander("Paste CSV text instead of a file"):
        pasted_csv = st.text_area(
            "CSV text",
            key="csv_text",
            height=140,
            placeholder="Company,Job Title,Job Description\nAcme,Backend Engineer,\"We are hiring ...\"",
        )

    parsed_jobs = None
    mapping: dict = {}
    warnings: list = []
    source = ""

    if uploaded_file is not None:
        csv_bytes = uploaded_file.getvalue()
        source = uploaded_file.name
    elif pasted_csv and pasted_csv.strip():
        csv_bytes = pasted_csv.encode("utf-8")
        source = "pasted text"
    else:
        csv_bytes = None

    if csv_bytes:
        try:
            parsed_jobs, mapping, warnings = parse_jobs_csv(csv_bytes)
        except ValueError as error:
            st.error(f"Could not read CSV: {error}")
            parsed_jobs = None
        else:
            if parsed_jobs:
                count = len(parsed_jobs)
                st.success(f"{count} job{'s' if count != 1 else ''} loaded from {source}")
            else:
                st.warning("No valid jobs found. Check the row warnings below.")

    if parsed_jobs:
        count = len(parsed_jobs)
        detail = f"{count} job{'s' if count != 1 else ''} loaded from {source}"
    elif csv_bytes:
        detail = "CSV could not be read"
    else:
        detail = "No CSV loaded yet. Upload a file or paste CSV text."

    banner_slot.markdown(
        status_banner("ready", detail, totals={"total": len(parsed_jobs or [])}),
        unsafe_allow_html=True,
    )
    if not server_key_ready:
        st.error("DEEPSEEK_API_KEY is not configured on this server. Add it and reload the studio.")

    if st.button("Start batch", type="primary", use_container_width=True, key="start_batch"):
        if not parsed_jobs:
            st.error("Upload a jobs CSV first.")
        elif not server_key_ready:
            st.error("DEEPSEEK_API_KEY is missing on this server.")
        else:
            try:
                config = RunConfig(
                    run_id=f"run_{uuid.uuid4().hex[:12]}",
                    provider=PROVIDER,
                    model=MODEL,
                    score_threshold=SCORE_THRESHOLD,
                    max_resumes=MAX_RESUMES,
                    reviewer_model=REVIEW_MODEL,
                    approval_threshold=APPROVAL_THRESHOLD,
                    max_drafts=MAX_DRAFTS,
                )
                new_run_id, new_code = run_service.create_run(
                    config,
                    st.session_state.profile,
                    [JobInput.from_dict(job) for job in parsed_jobs],
                )
            except (RuntimeError, ValueError) as error:
                st.error(str(error))
            else:
                clear_results()
                st.session_state["current_run_id"] = new_run_id
                st.session_state["current_recovery_code"] = new_code
                batch_runner.start_batch(new_run_id, new_code)
                st.rerun()

    if parsed_jobs:
        cost = estimate_batch_cost(
            total_jobs=len(parsed_jobs),
            max_resumes=MAX_RESUMES,
            expected_shortlist_rate=0.50,
        )
        st.caption(
            f"Estimated load: ~{cost['total_tokens']:,} tokens · {cost['cost_display']} · "
            f"~{cost['estimated_tailored']} tailored resumes"
        )
        with st.expander(
            f"Preview and column mapping · {len(parsed_jobs)} job{'s' if len(parsed_jobs) != 1 else ''}"
        ):
            st.caption("Detected columns: " + " · ".join(f"{key}: {value}" for key, value in mapping.items()))
            st.markdown(
                table_html(pd.DataFrame([
                    {
                        "Company": job["company"],
                        "Role": job["title"],
                        "Location": job["location"],
                        "Description characters": len(job["jd_text"]),
                    }
                    for job in parsed_jobs[:5]
                ])),
                unsafe_allow_html=True,
            )
    if warnings:
        with st.expander(f"{len(warnings)} row warnings"):
            for warning in warnings:
                st.text(warning)

# ==========================================
# SCREEN 2: RUNNING - live progress
# ==========================================
elif screen == "running":
    titles = {
        "running": "Batch running",
        "pending": "Batch starting",
        "paused": "Batch paused",
        "awaiting_credentials": "Batch paused",
    }
    st.title(titles.get(run_status, "Batch running"))

    total_jobs = int(active_run_state.get("total_jobs", 0))
    scored = int(active_run_state.get("completed_count", 0))
    stage = str(events[0]["stage"]) if events else ""
    latest = str(events[0]["message"]) if events else "Starting the background batch"
    verified = int(active_run_state.get("verified_resumes_count", 0))

    banner_status = "running" if run_status in ("running", "pending") else "paused"
    st.markdown(
        status_banner(
            banner_status,
            f"{scored} of {total_jobs} jobs scored" + (f" · Stage: {stage}" if stage else ""),
            totals={
                "total": total_jobs,
                "scored": scored,
                "approved": verified,
                "run_id": run_id,
                "stage": stage,
                "recovery_code": recovery_code,
            },
        ),
        unsafe_allow_html=True,
    )

    st.progress(min(1.0, scored / max(1, total_jobs)), text=f"{scored}/{total_jobs} jobs scored")

    start_error = batch_runner.get_error(run_id)
    if start_error:
        st.session_state["start_error"] = start_error
    if st.session_state.get("start_error"):
        st.error(st.session_state["start_error"])

    if run_status != "running":
        if run_status == "pending":
            st.warning("Waiting for the worker to pick up the batch.")
        else:
            st.warning(f"Batch paused. {latest}")
        if st.button("Resume batch", type="primary", use_container_width=True, key="resume_batch"):
            batch_runner.start_batch(run_id, recovery_code)
            st.rerun()

    if events:
        st.markdown(
            render_live_log_console([event["message"] for event in reversed(events)]),
            unsafe_allow_html=True,
        )

    if run_status == "running" and st.button("Pause batch", use_container_width=True, key="pause_batch"):
        run_service.pause_run(run_id, recovery_code)
        st.info("Pause requested. The batch saves its progress and stops after the current step.")

    if is_running:
        time.sleep(2)
        st.rerun()

# ==========================================
# SCREEN 3: COMPLETE - review and download
# ==========================================
else:
    results = st.session_state.batch_results or []
    batch_counts = counts(results)
    avg_score = round(sum(r.get("match_score", 0) for r in results) / len(results), 1) if results else 0
    failed = run_status == "failed"
    downloads = {
        "zip": bool(st.session_state.get("zip_bytes")),
        "summary_csv": bool(st.session_state.get("summary_csv")),
        "html_report": bool(st.session_state.get("summary_html")),
    }

    st.title("Batch failed" if failed else "Batch complete")
    processed = batch_counts["processed"]
    approved_count = batch_counts["approved"]
    st.markdown(
        status_banner(
            "failed" if failed else "complete",
            f"{processed} job{'s' if processed != 1 else ''} processed · "
            f"{approved_count} approved PDF{'s' if approved_count != 1 else ''}"
            + (f" · {batch_counts['attention']} need attention" if batch_counts["attention"] else ""),
            totals={
                "total": batch_counts["processed"],
                "scored": batch_counts["processed"],
                "approved": batch_counts["approved"],
                "attention": batch_counts["attention"],
                "zip": downloads["zip"],
                "run_id": run_id,
                "stage": "Complete" if not failed else "Interrupted",
                "recovery_code": recovery_code,
            },
        ),
        unsafe_allow_html=True,
    )

    if failed:
        latest = str(events[0]["message"]) if events else "The batch stopped before finishing."
        st.error(f"Batch interrupted. {latest}")
        if st.button("Resume batch", type="primary", use_container_width=True, key="resume_batch"):
            batch_runner.start_batch(run_id, recovery_code)
            st.rerun()

    metrics = st.columns(4)
    metrics[0].metric("Jobs processed", batch_counts["processed"])
    metrics[1].metric("Approved PDFs" if review_required else "PDFs generated", batch_counts["generated"])
    metrics[2].metric("Average match", f"{avg_score}%")
    metrics[3].metric("Needs attention", batch_counts["attention"])

    download_cols = st.columns(3)
    with download_cols[0]:
        if downloads["zip"]:
            st.download_button(
                "Download ZIP",
                st.session_state.zip_bytes,
                file_name="jobhunt_tailored_batch.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
                key="download_zip",
            )
        else:
            st.caption("No ZIP bundle was produced.")
    with download_cols[1]:
        if downloads["summary_csv"]:
            st.download_button(
                "Download summary CSV",
                st.session_state.summary_csv,
                file_name="batch_applied_summary.csv",
                mime="text/csv",
                use_container_width=True,
                key="download_summary_csv",
            )
    with download_cols[2]:
        if downloads["html_report"]:
            st.download_button(
                "Download HTML report",
                st.session_state.summary_html,
                file_name="shortlisted_jobs_report.html",
                mime="text/html",
                use_container_width=True,
                key="download_html_report",
            )

    if st.session_state.get("package_error"):
        st.warning(f"Export bundle unavailable: {st.session_state.package_error}")

    if results:
        st.subheader("Jobs")
        # A plain HTML table (not a canvas grid) keeps every value readable.
        st.markdown(
            table_html(pd.DataFrame([
                {
                    "Company": r.get("company"),
                    "Role": r.get("title"),
                    "Match": f"{r.get('match_score', 0)}%",
                    "Challenger": r.get("challenger_score") if r.get("challenger_score") is not None else "-",
                    "Status": display_status(r),
                    "Location": r.get("location"),
                }
                for r in results
            ])),
            unsafe_allow_html=True,
        )

        labels = [
            f"{index + 1}. {r.get('company')} — {r.get('title')} ({r.get('match_score', 0)}%)"
            for index, r in enumerate(results)
        ]
        chosen = st.selectbox("Inspect a job", labels, key="inspect_job")
        result = results[labels.index(chosen)]

        st.caption(
            f"Status: {display_status(result)} · Reason: {result.get('reason') or 'not recorded'} · "
            f"Missing skills: {', '.join(result.get('missing_skills', [])) or 'none recorded'}"
        )
        raw_url = str(result.get("url", ""))
        if is_safe_url(raw_url):
            st.link_button("Open job posting", raw_url)

        pdf_content = approved_pdf_bytes(result) if review_required else None
        pdf_path = result.get("pdf_path") if pdf_content is not None else result.get("draft_path")
        pdf_action, check_action = st.columns(2)
        with pdf_action:
            if pdf_content is not None:
                st.download_button(
                    "Download PDF",
                    pdf_content,
                    file_name=Path(result.get("pdf_path") or "resume.pdf").name,
                    mime="application/pdf",
                    use_container_width=True,
                    key="download_pdf",
                )
            elif pdf_path and Path(pdf_path).is_file():
                st.download_button(
                    "Download PDF",
                    Path(pdf_path).read_bytes(),
                    file_name=Path(pdf_path).name,
                    mime="application/pdf",
                    use_container_width=True,
                    key="download_pdf",
                )
            else:
                st.caption(f"No PDF available for this job ({display_status(result)}).")
        with check_action:
            if pdf_path and Path(pdf_path).is_file():
                check_id = f"{result.get('job_id')}:{Path(pdf_path).stat().st_mtime_ns}"
                checks = st.session_state.setdefault("pdf_checks", {})
                if st.button("Check PDF", use_container_width=True, key="check_pdf"):
                    try:
                        checks[check_id] = audit_resume_pdf(str(pdf_path))
                    except Exception as error:  # pragma: no cover - defensive
                        checks[check_id] = {"passed": False, "errors": [str(error)]}
                check = checks.get(check_id)
                if check:
                    if check.get("passed"):
                        st.success(f"PDF checks passed · {check.get('details', {}).get('page_count', '?')} page(s).")
                    else:
                        st.warning("PDF checks flagged: " + "; ".join(check.get("errors", [])))
                    for warning in check.get("warnings", []):
                        st.caption(warning)

        with st.expander("Job description"):
            st.write(result.get("jd_text") or "No description recorded.")
        with st.expander("Tailored resume content"):
            tailored = result.get("tailored_resume")
            if tailored:
                st.write(tailored.get("profile_summary") or "Summary tailored to this role.")
                skills = tailored.get("skills", [])
                if skills:
                    st.caption("Prioritised skills: " + ", ".join(skills[:10]))
            else:
                st.caption("No tailored content was stored for this job.")
        with st.expander("Challenger findings"):
            attempts = result.get("attempts", [])
            if not attempts:
                st.caption("No review attempts recorded.")
            for attempt in attempts:
                review = attempt.get("review")
                if not review:
                    continue
                verdict = "approved" if review.get("approved") else "needs changes"
                st.text(f"Draft {attempt.get('number')} · {review.get('score', 0):.1f}/100 · {verdict}")
                for finding in review.get("findings", []):
                    st.text(f"{finding['section']} · {finding['evidence']} · {finding['correction']}")
                for error in (attempt.get("audit") or {}).get("errors", []):
                    st.text(error)
            for warning in (result.get("company_context") or {}).get("warnings", []):
                st.caption(warning)

    else:
        st.info("No jobs were processed in this batch.")

    if st.button("Start a new batch", use_container_width=True, key="new_batch"):
        clear_results()
        st.rerun()

# ==========================================
# SHARED: owner utilities
# ==========================================
def render_owner_tools() -> None:
    """Read-only profile, recovery lookup and the lock action."""
    profile = st.session_state.profile
    info = profile.get("personal_information", {})

    with st.expander("Master profile (read-only)"):
        st.caption(
            f"{info.get('name')} {info.get('surname')} · {info.get('email')} · "
            f"{len(profile.get('skills', []))} skills · {len(profile.get('projects', []))} projects"
        )
        st.write(profile.get("profile_summary", "No summary recorded."))
        st.caption("Editing, uploading and replacing the profile are disabled by design.")
        st.download_button(
            "Download profile (.yaml)",
            profile_to_yaml_str(profile),
            file_name="kaustubh_kale_master_profile.yaml",
            mime="text/yaml",
            use_container_width=True,
            key="download_profile",
        )

    with st.expander("Recovery, runs and lock"):
        st.caption(f"Session: {session_id} · Contact: {OWNER_EMAIL}")
        if recovery_code:
            st.text("Recovery code for this batch")
            st.code(recovery_code, language=None)
        lookup_code = st.text_input(
            "Recovery code",
            placeholder="jh-xxxx-xxxx-xxxx",
            key="recovery_code_input",
        )
        if st.button("Find run", use_container_width=True, key="find_run") and lookup_code:
            code_hash = hash_recovery_code(lookup_code.strip())
            with run_store_instance._get_connection() as conn:
                matched = conn.execute(
                    "SELECT * FROM runs WHERE recovery_code_hash = ?", (code_hash,)
                ).fetchone()
            if matched:
                st.session_state["inspected_run"] = dict(matched)
                st.session_state["inspected_code"] = lookup_code.strip()
                st.success(f"Run {matched['run_id']} found ({matched['status']})")
            else:
                st.session_state.pop("inspected_run", None)
                st.error("No run found for this recovery code.")
        if "inspected_run" in st.session_state:
            inspected = run_store_instance.get_run(st.session_state["inspected_run"]["run_id"])
            inspected_code = st.session_state.get("inspected_code")
            if inspected and inspected_code:
                st.text(
                    f"Run {inspected['run_id']} · {inspected['status']} · "
                    f"{inspected['completed_count']}/{inspected['total_jobs']} scored"
                )
                if st.button("Load these results", use_container_width=True, key="load_run"):
                    clear_results()
                    st.session_state["current_run_id"] = inspected["run_id"]
                    st.session_state["current_recovery_code"] = inspected_code
                    st.rerun()
                if st.button("Delete this run", use_container_width=True, key="delete_lookup_run"):
                    try:
                        run_service.delete_run(inspected["run_id"], inspected_code)
                    except (RuntimeError, PermissionError) as error:
                        st.error(str(error))
                    else:
                        st.session_state.pop("inspected_run", None)
                        st.session_state.pop("inspected_code", None)
                        if inspected["run_id"] == run_id:
                            clear_results()
                        st.rerun()
        if st.button("Lock studio", use_container_width=True, key="lock_studio"):
            st.session_state.clear()
            st.rerun()
        st.download_button(
            "Download sample jobs CSV",
            generate_sample_csv_str(),
            file_name="sample_jobs_template.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_sample_csv",
        )


if screen in ("ready", "complete"):
    render_owner_tools()
