"""JobHunt AI Studio: Decoupled presentation and execution studio."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from core.ai_engine import ai_engine_instance
from core.ats_verifier import audit_resume_pdf
from core.cost_estimator import estimate_batch_cost
from core.csv_parser import generate_sample_csv_str, parse_jobs_csv
from core.health import perform_system_health_check
from core.models import ArtifactResult, JobInput, JobResult, RunConfig, RunStatus, Stage
from core.packager import build_batch_zip_bytes
from core.pdf_generator import generate_single_page_pdf
from core.profile_manager import (
    DEFAULT_PROFILE_TEMPLATE,
    load_profile_from_file,
    load_profile_from_str,
    normalize_profile,
    profile_to_yaml_str,
)
from core.resume_tailor import tailor_resume_async
from core.run_service import RunService
from core.run_store import run_store_instance
from core.scorer import score_job_async
from core.security import (
    build_safe_pdf_filename,
    compute_input_fingerprint,
    compute_stable_job_id,
    generate_recovery_code,
    hash_recovery_code,
    is_safe_url,
)
from core.session_manager import session_manager
from core.summary_reporter import generate_summary_csv, render_summary_html
from ui import (
    candidate_card,
    counts,
    display_status,
    filter_results,
    metric_strip,
    pipeline_html,
    render_ats_checklist,
    render_health_card,
    result_group,
    section,
    styles,
)

# Page configuration & design system injection
st.set_page_config(
    page_title="JobHunt AI Studio",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="auto",
)
styles()

# Instantiate backend orchestrator
run_service = RunService(store=run_store_instance)


def run_coro_sync(coro):
    """Executes an async coroutine synchronously inside the Streamlit script loop."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(coro)


def sample_profile() -> Dict[str, Any]:
    """Loads the bundled fictional candidate profile."""
    sample = Path(__file__).parent / "data" / "sample_profile.yaml"
    try:
        return load_profile_from_file(sample)
    except Exception:
        return normalize_profile(DEFAULT_PROFILE_TEMPLATE)


def clear_results() -> None:
    """Resets all results, artifacts, and search state."""
    for key in (
        "batch_results",
        "zip_bytes",
        "summary_csv",
        "pipeline_view",
        "pipeline_progress",
        "pdf_checks",
        "result_query",
        "result_group",
        "current_run_id",
        "current_recovery_code",
    ):
        st.session_state.pop(key, None)


def apply_profile(profile: Dict[str, Any]) -> None:
    """Applies a candidate profile to the active session state."""
    st.session_state.profile = profile
    st.session_state.profile_editor_pending = profile_to_yaml_str(profile)
    st.session_state.profile_notice = "Profile applied. Your next batch will use these details."


# Background maintenance & session setup
try:
    session_manager.cleanup_expired_sessions()
except Exception:
    pass

if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex[:12]
session_id = st.session_state.session_id
session_dir = session_manager.get_session_dir(session_id)
resumes_dir = session_manager.get_resumes_dir(session_id)

if "profile" not in st.session_state:
    st.session_state.profile = sample_profile()
if "profile_editor_pending" in st.session_state:
    st.session_state.profile_editor = st.session_state.pop("profile_editor_pending")
for key in ("batch_results", "zip_bytes"):
    st.session_state.setdefault(key, None)

# ==========================================
# SIDEBAR: Providers, Keys, Settings & Health
# ==========================================
with st.sidebar:
    st.markdown('<div class="brand"><span class="brand-mark">JH</span> JobHunt AI Studio</div>', unsafe_allow_html=True)
    st.caption("Intelligent resume tailoring & batch pipeline")
    st.divider()

    # System Health Diagnostics
    try:
        health_info = perform_system_health_check()
        st.markdown(render_health_card(health_info), unsafe_allow_html=True)
    except Exception:
        pass
    st.divider()

    st.markdown("##### AI connection")
    provider = st.selectbox("AI Provider", ["Google Gemini", "DeepSeek", "OpenAI"])
    api_keys_list: List[str] = []
    is_gemini_free = False
    gemini_rpm = 15

    if provider == "Google Gemini":
        with st.expander("Model & quota settings", expanded=False):
            gemini_tier = st.radio("Gemini account tier", ["Free Tier", "Paid Tier"])
            is_gemini_free = gemini_tier == "Free Tier"
            if is_gemini_free:
                selected_model = "gemini-3.5-flash-lite"
                st.caption("Gemini 3.5 Flash-Lite · Free Tier cap: 15 RPM. Provider rate limits apply.")
            else:
                selected_model = st.selectbox("Gemini Paid Model", ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"])
                gemini_rpm = st.number_input("Max RPM Cap", min_value=15, max_value=300, value=60)
        if is_gemini_free:
            raw_keys = st.text_input(
                "Gemini API keys",
                value=os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", "")),
                type="password",
                help="Paste one key, or separate multiple keys with commas.",
            )
            api_keys_list = [k.strip() for k in raw_keys.replace("\n", ",").split(",") if k.strip()]
        else:
            single_key = st.text_input(
                "Gemini Paid API Key",
                value=os.environ.get("GEMINI_API_KEY", ""),
                type="password",
            )
            api_keys_list = [single_key.strip()] if single_key.strip() else []
        ai_engine_instance.configure_keys(provider="gemini", gemini_keys=api_keys_list, gemini_rpm=gemini_rpm)
    elif provider == "DeepSeek":
        with st.expander("Model settings"):
            selected_model = st.selectbox("DeepSeek Model", ["deepseek-chat", "deepseek-reasoner"])
        ds_key = st.text_input("DeepSeek API Key", value=os.environ.get("DEEPSEEK_API_KEY", ""), type="password")
        api_keys_list = [ds_key.strip()] if ds_key.strip() else []
        ai_engine_instance.configure_keys(provider="deepseek", deepseek_key=ds_key)
    else:
        with st.expander("Model settings"):
            selected_model = st.selectbox("OpenAI Model", ["gpt-4o-mini", "gpt-4o"])
        oa_key = st.text_input("OpenAI API Key", value=os.environ.get("OPENAI_API_KEY", ""), type="password")
        api_keys_list = [oa_key.strip()] if oa_key.strip() else []
        ai_engine_instance.configure_keys(provider="openai", openai_key=oa_key)

    st.caption(f"{len(api_keys_list)} key(s) entered · {selected_model}" if api_keys_list else "Add an API key to enable your batch.")
    st.divider()

    # Recovery Code Lookup & Management
    with st.expander("Run recovery & lookup", expanded=False):
        st.caption("Inspect, resume, or download previous batch runs using a 12-character recovery code.")
        lookup_code = st.text_input("Batch recovery code", placeholder="jh-xxxx-xxxx-xxxx", key="sidebar_recovery_code")
        if st.button("Inspect run", key="btn_inspect_run", use_container_width=True) and lookup_code:
            code_hash = hash_recovery_code(lookup_code.strip())
            with run_store_instance._get_connection() as conn:
                matched_row = conn.execute("SELECT * FROM runs WHERE recovery_code_hash = ?", (code_hash,)).fetchone()
            if matched_row:
                st.session_state["inspected_run"] = dict(matched_row)
                st.session_state["inspected_code"] = lookup_code.strip()
                st.success(f"Run {matched_row['run_id']} found ({matched_row['status']})")
            else:
                st.session_state.pop("inspected_run", None)
                st.error("No run found matching this recovery code.")

        if "inspected_run" in st.session_state:
            irun = st.session_state["inspected_run"]
            icode = st.session_state["inspected_code"]
            st.markdown(f"**Run ID:** `{irun['run_id']}`")
            st.markdown(f"**Status:** `{irun['status']}`")
            st.markdown(f"**Jobs:** {irun['completed_count']} / {irun['total_jobs']} completed")
            st.markdown(f"**Verified Resumes:** {irun['verified_resumes_count']}")
            if st.button("Delete run & artifacts", key="btn_delete_inspected", use_container_width=True):
                try:
                    run_service.delete_run(irun["run_id"], icode)
                    st.session_state.pop("inspected_run", None)
                    st.success("Run purged successfully.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not delete run: {e}")

    with st.expander("Session & storage"):
        st.caption(f"Session: {session_id}")
        st.caption("Local workspace files expire automatically.")
        if st.button("Delete session files", use_container_width=True):
            session_manager.cleanup_session(session_id)
            session_manager.clear_checkpoint(session_id)
            clear_results()
            st.rerun()

# ==========================================
# MAIN WORKSPACE HEADER & TABS
# ==========================================
st.markdown('<span class="eyebrow">YOUR NEXT CHAPTER, PREPARED.</span>', unsafe_allow_html=True)
st.title("Make every application count.")
st.markdown(
    '<p class="hero-copy">Match your experience to the right roles. Create verified single-page tailored resumes in one focused workflow.</p>',
    unsafe_allow_html=True,
)
tab_batch, tab_profile, tab_guide = st.tabs(["Workspace", "Candidate profile", "Guide & templates"])

# ------------------------------------------
# TAB: CANDIDATE PROFILE
# ------------------------------------------
with tab_profile:
    section("01", "Your experience, in one place", "Keep your master profile accurate. It is the source for matching and tailoring.")
    if notice := st.session_state.pop("profile_notice", None):
        st.success(notice)
    candidate_card(st.session_state.profile)

    left_prof, right_prof = st.columns([1, 1], gap="large")
    with left_prof:
        with st.container(border=True):
            st.markdown("##### Import a profile")
            prof_file = st.file_uploader(
                "Master profile",
                type=["yaml", "yml", "json"],
                help="Upload YAML or JSON, then apply it explicitly.",
            )
            if st.button("Apply uploaded profile", disabled=prof_file is None, use_container_width=True):
                try:
                    apply_profile(load_profile_from_str(prof_file.getvalue().decode("utf-8-sig")))
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not apply profile: {exc}")
    with right_prof:
        with st.container(border=True):
            st.markdown("##### Active profile")
            info = st.session_state.profile.get("personal_information", {})
            for label, key in (("Email", "email"), ("Phone", "phone"), ("Location", "address")):
                st.text(f"{label}: {info.get(key) or 'Not provided'}")
            st.caption("Target roles: " + ", ".join(st.session_state.profile.get("target_roles", [])))
            st.download_button(
                "Download profile YAML",
                profile_to_yaml_str(st.session_state.profile),
                file_name="master_profile.yaml",
                mime="text/yaml",
                use_container_width=True,
            )

    with st.expander("Edit profile YAML", expanded=False):
        st.session_state.setdefault("profile_editor", profile_to_yaml_str(st.session_state.profile))
        with st.form("profile_form"):
            edited_yaml = st.text_area(
                "Profile YAML",
                key="profile_editor",
                height=360,
                help="Changes are used only after you apply them.",
            )
            apply_edit = st.form_submit_button("Apply profile changes", type="primary")
        if apply_edit:
            try:
                st.session_state.profile = load_profile_from_str(edited_yaml)
                st.session_state.profile_notice = "Profile applied. Your next batch will use these details."
                st.rerun()
            except Exception as exc:
                st.error(f"Could not apply changes: {exc}")

    with st.expander("Sample profile"):
        st.caption("Load the bundled sample to explore the workflow. Replace its details before creating your own applications.")
        if st.button("Load sample profile"):
            apply_profile(sample_profile())
            st.rerun()

# ------------------------------------------
# TAB: GUIDE & TEMPLATES
# ------------------------------------------
with tab_guide:
    section("01", "Start with the right inputs", "Upload a jobs CSV and a YAML or JSON candidate profile.")
    st.download_button(
        "Download sample jobs CSV",
        generate_sample_csv_str(),
        file_name="sample_jobs_template.csv",
        mime="text/csv",
        type="primary",
    )
    st.dataframe(
        pd.DataFrame([
            {"Field": "Company", "Required": "Yes", "Accepted headers": "Company, employer, organization"},
            {"Field": "Job title", "Required": "Yes", "Accepted headers": "Title, job title, role, position"},
            {"Field": "Description", "Required": "Yes", "Accepted headers": "Job description, jd_text, description, content"},
            {"Field": "Apply URL", "Required": "No", "Accepted headers": "URL, job link, apply_url"},
            {"Field": "Location", "Required": "No", "Accepted headers": "Location, city, work_mode"},
        ]),
        hide_index=True,
        use_container_width=True,
    )
    with st.expander("How the 6-stage pipeline works", expanded=True):
        st.markdown(
            "1. **Validate**: Fuzzy column mapping and description safety checks.\n"
            "2. **Score**: Profile matching against full JD context with truthful failure detection.\n"
            "3. **Tailor**: Fact-bound resume alignment without hallucinatory skill invention.\n"
            "4. **Render**: Playwright Chromium single-page PDF rendering.\n"
            "5. **Check**: Strict 1-page bounds, clean URLs, 4-digit years, and kerning audit.\n"
            "6. **Package**: Verified PDFs, summary CSV, and HTML report bundled into ZIP archive."
        )
    with st.expander("Quotas, recovery & reviewing outputs"):
        st.write("The decoupled engine enforces provider limits and recovery checkpoints. Runs can be resumed across server restarts using your recovery code.")
        st.write("Always review tailored content before submitting applications. Single-page PDFs undergo automated verification against ATS layout standards.")

# ------------------------------------------
# TAB: WORKSPACE (Setup -> Live Pipeline -> Results)
# ------------------------------------------
with tab_batch:
    # --------------------------------------
    # STAGE 1: SETUP
    # --------------------------------------
    section("01", "Set up your batch", "Choose your inputs and set a limit before you start.")
    c_full_name = candidate_card(st.session_state.profile)

    upload_col, settings_col = st.columns([1.7, 1], gap="large")
    parsed_jobs = None

    with upload_col:
        uploaded_file = st.file_uploader(
            "Jobs CSV",
            type=["csv"],
            help="Include company, job title and a full description (at least 50 characters).",
        )
        if uploaded_file is not None:
            try:
                parsed_jobs, mapping, warnings = parse_jobs_csv(uploaded_file.getvalue())
                if parsed_jobs:
                    st.success(f"{len(parsed_jobs)} jobs ready from {uploaded_file.name}")
                else:
                    st.warning("No valid jobs found. Check the descriptions and row warnings below.")
                with st.expander(f"Preview & column mapping · {len(parsed_jobs)} jobs"):
                    st.caption(" · ".join(f"{k}: {v}" for k, v in mapping.items()))
                    st.dataframe(
                        pd.DataFrame([
                            {
                                "Company": j["company"],
                                "Role": j["title"],
                                "Location": j["location"],
                                "Description characters": len(j["jd_text"]),
                            }
                            for j in parsed_jobs[:5]
                        ]),
                        hide_index=True,
                        use_container_width=True,
                    )
                if warnings:
                    with st.expander(f"{len(warnings)} row warnings"):
                        for warning in warnings:
                            st.text(warning)
            except Exception as exc:
                st.error(f"Could not read CSV: {exc}")
                parsed_jobs = None
        else:
            st.caption("Company, role and description required. Common column names are detected automatically.")

    with settings_col:
        with st.container(border=True):
            score_threshold = st.slider(
                "Match threshold (%)",
                40,
                90,
                70,
                5,
                help="Generate resumes for jobs at or above this score.",
            )
            max_resumes = st.slider(
                "Resume limit",
                1,
                100,
                20,
                help="Maximum resume generation attempts per batch.",
            )

    if parsed_jobs:
        cost = estimate_batch_cost(
            total_jobs=len(parsed_jobs),
            provider=provider,
            model=selected_model,
            is_gemini_free=is_gemini_free,
            max_resumes=max_resumes,
            expected_shortlist_rate=0.50,
        )
        budget_label = cost["cost_display"].replace("🎉 ", "")
        st.markdown(
            f'<div class="estimate">'
            f'<strong>Estimated budget · {escape(budget_label)}</strong>'
            f'<span>~{cost["total_tokens"]:,} tokens · ~{cost["estimated_tailored"]} resumes</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.caption("Planning estimate from the cost estimator. Actual provider usage and token billing may vary.")

    existing_checkpoint = session_manager.load_checkpoint(session_id)
    checkpoint_completed_jobs = []
    checkpoint_resume_index = 0
    if existing_checkpoint and parsed_jobs:
        completed = existing_checkpoint.get("completed_jobs", [])
        if 0 < len(completed) < len(parsed_jobs):
            checkpoint_completed_jobs = completed
            checkpoint_resume_index = len(completed)
            st.warning(f"Checkpoint available: {len(completed)} jobs processed. Resume only with the same CSV, profile and settings.")

    can_run = bool(parsed_jobs) and bool(api_keys_list)
    missing = []
    if not parsed_jobs:
        missing.append("upload a valid jobs CSV")
    if not api_keys_list:
        missing.append("add your API key in the sidebar")
    st.caption("To start, " + " and ".join(missing) + "." if missing else "Ready to run with your active candidate profile.")

    actions = st.columns([2, 1, 1] if checkpoint_resume_index else [3, 1])
    with actions[0]:
        btn_run = st.button("Start batch", type="primary", use_container_width=True, disabled=not can_run)
    btn_resume = False
    if checkpoint_resume_index:
        with actions[1]:
            btn_resume = st.button(
                f"Resume at job {checkpoint_resume_index + 1}",
                disabled=not can_run,
                use_container_width=True,
            )
    with actions[-1]:
        if st.button("Clear results", use_container_width=True):
            session_manager.clear_checkpoint(session_id)
            clear_results()
            st.rerun()

    # --------------------------------------
    # STAGE 2: LIVE PIPELINE
    # --------------------------------------
    section("02", "Live pipeline", "Stages track the current job. Counters track the whole batch.")
    pipeline_box = st.empty()
    rate_note = "Gemini Free Tier active (15 RPM cap: ~4s/call) · Auto-cooldown active" if is_gemini_free else None
    pipeline_box.markdown(
        st.session_state.get(
            "pipeline_view",
            pipeline_html(total=len(parsed_jobs or []), rate_limit_note=rate_note),
        ),
        unsafe_allow_html=True,
    )
    progress_bar = st.progress(
        st.session_state.get("pipeline_progress", 0),
        text="Completed jobs; packaging is tracked separately.",
    )
    status_box = st.empty()

    if (btn_run or btn_resume) and parsed_jobs is not None:
        log_box = st.expander("Execution details", expanded=False)
        results_accumulator = list(checkpoint_completed_jobs) if btn_resume else []
        start_index = checkpoint_resume_index if btn_resume else 0
        if not btn_resume:
            session_manager.clear_checkpoint(session_id)
        clear_results()

        # Generate cryptographic recovery code for the run
        plain_recovery_code = generate_recovery_code()
        st.session_state["current_recovery_code"] = plain_recovery_code
        st.session_state["current_run_id"] = session_id
        st.info(f"Batch Recovery Code: `{plain_recovery_code}` · Save this code to check or resume your run later.")

        total_to_process = len(parsed_jobs)
        started = time.monotonic()
        current_stage = "Scoring"
        current_job: Dict[str, Any] = {}

        def update_pipeline(stage, company, role, complete=False, failed=False):
            st.session_state.pipeline_view = pipeline_html(
                stage=stage,
                company=company,
                role=role,
                results=results_accumulator,
                total=total_to_process,
                complete=complete,
                failed=failed,
                rate_limit_note=rate_note,
            )
            st.session_state.pipeline_progress = (
                1.0 if complete else (len(results_accumulator) / total_to_process if total_to_process else 0)
            )
            pipeline_box.markdown(st.session_state.pipeline_view, unsafe_allow_html=True)
            elapsed = int(time.monotonic() - started)
            progress_bar.progress(
                st.session_state.pipeline_progress,
                text=f"{len(results_accumulator)} of {total_to_process} jobs processed · {elapsed}s elapsed",
            )

        tailored_count = sum(1 for r in results_accumulator if r.get("pdf_path"))

        try:
            for i in range(start_index, total_to_process):
                job = parsed_jobs[i]
                current_job = job
                current_stage = "Scoring"
                update_pipeline(current_stage, job["company"], job["title"])
                log_box.write(f"Scoring {i + 1}/{total_to_process}: {job['company']} — {job['title']}")

                clean_provider = (
                    "gemini"
                    if "gemini" in provider.lower()
                    else ("deepseek" if "deepseek" in provider.lower() else "openai")
                )

                scored = run_coro_sync(
                    score_job_async(
                        job=job,
                        profile=st.session_state.profile,
                        model=selected_model,
                        provider=clean_provider,
                        api_keys=api_keys_list,
                    )
                )
                score = scored.get("match_score", 0)

                if score >= score_threshold and tailored_count < max_resumes:
                    tailored_count += 1
                    current_stage = "Tailoring"
                    update_pipeline(current_stage, job["company"], job["title"])
                    log_box.write(f"Tailoring: {job['company']} · match {score}%")

                    tailored_resume = run_coro_sync(
                        tailor_resume_async(
                            job=scored,
                            profile=st.session_state.profile,
                            model=selected_model,
                            provider=clean_provider,
                            api_keys=api_keys_list,
                        )
                    )
                    scored["tailored_resume"] = tailored_resume

                    current_stage = "PDF generation"
                    update_pipeline(current_stage, job["company"], job["title"])
                    try:
                        pdf_path = generate_single_page_pdf(
                            tailored_resume=tailored_resume,
                            output_dir=resumes_dir,
                            job=scored,
                        )
                        scored["pdf_path"] = pdf_path
                        scored["pdf_filename"] = os.path.basename(pdf_path)
                        scored["status"] = "Tailored & Verified"
                        log_box.write(f"PDF saved: {os.path.basename(pdf_path)}")
                    except Exception as exc:
                        scored["pdf_path"] = None
                        scored["pdf_filename"] = None
                        scored["status"] = f"Render Error: {str(exc)[:30]}"
                        log_box.error(f"PDF generation failed for {job['company']}: {exc}")
                else:
                    scored["pdf_path"] = None
                    scored["pdf_filename"] = None
                    scored["status"] = "Below Threshold" if score < score_threshold else "Cap Reached"
                    log_box.write(f"Skipped: {job['company']} · {scored['status']}")

                results_accumulator.append(scored)
                session_manager.save_checkpoint(
                    session_id=session_id,
                    total_jobs=total_to_process,
                    completed_jobs=results_accumulator,
                    threshold=score_threshold,
                    model=selected_model,
                    provider=provider,
                )
                update_pipeline(None, job["company"], f"Job {i + 1} processed · {display_status(scored)}")

            current_stage = "Packaging"
            update_pipeline(current_stage, "Preparing your downloads", "Building the CSV summary, HTML report and ZIP bundle.")
            summary_csv_str = generate_summary_csv(results_accumulator)
            summary_html_str = render_summary_html(
                results=results_accumulator,
                candidate_name=c_full_name,
                threshold=score_threshold,
            )
            all_pdf_paths = [r["pdf_path"] for r in results_accumulator if r.get("pdf_path")]
            zip_bytes = build_batch_zip_bytes(
                pdf_paths=all_pdf_paths,
                summary_csv_str=summary_csv_str,
                summary_html_str=summary_html_str,
            )

            session_manager.clear_checkpoint(session_id)
            st.session_state.batch_results = results_accumulator
            st.session_state.zip_bytes = zip_bytes
            st.session_state.summary_csv = summary_csv_str
            update_pipeline(None, "Your batch is ready", "Review job details and download your files below.", complete=True)

            if counts(results_accumulator)["attention"]:
                status_box.warning("Batch finished with jobs needing attention. Review their details below.")
            else:
                status_box.success("Batch finished. Your downloads are ready.")

        except Exception as exc:
            st.session_state.batch_results = results_accumulator or None
            update_pipeline(
                current_stage,
                current_job.get("company", "Batch interrupted"),
                "Processing stopped. See the error below.",
                failed=True,
            )
            status_box.error(f"Batch interrupted: {exc}. Completed work is shown below; check the checkpoint before retrying.")

    # --------------------------------------
    # STAGE 3: RESULTS (Review & Download)
    # --------------------------------------
    section("03", "Review & download", "Inspect the match, review each resume and take your next step.")
    if st.session_state.batch_results:
        results = st.session_state.batch_results
        c = counts(results)
        avg_score = round(sum(r.get("match_score", 0) for r in results) / len(results), 1) if results else 0
        st.markdown(
            metric_strip((
                ("Jobs processed", c["processed"]),
                ("PDFs generated", c["generated"]),
                ("Average match", f"{avg_score}%"),
                ("Needs attention", c["attention"]),
            )),
            unsafe_allow_html=True,
        )

        download_cols = st.columns([2, 1])
        with download_cols[0]:
            if st.session_state.zip_bytes:
                st.download_button(
                    f"Download ZIP · {c['generated']} resumes + reports",
                    st.session_state.zip_bytes,
                    file_name="jobhunt_tailored_batch.zip",
                    mime="application/zip",
                    type="primary",
                    use_container_width=True,
                )
        with download_cols[1]:
            if st.session_state.get("summary_csv"):
                st.download_button(
                    "Download summary CSV",
                    st.session_state.summary_csv,
                    file_name="batch_applied_summary.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

        search_col, filter_col = st.columns([2, 1])
        with search_col:
            query = st.text_input("Search jobs", placeholder="Company, role or location", key="result_query")
        with filter_col:
            group = st.selectbox(
                "Show",
                ["All jobs", "PDF generated", "Skipped", "Needs attention"],
                key="result_group",
            )

        filtered = filter_results(results, query, group)
        st.caption(f"Showing {len(filtered)} of {len(results)} jobs")

        if not filtered:
            st.info("No jobs match these filters. Try another search or choose All jobs.")
        else:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Company": r.get("company"),
                        "Role": r.get("title"),
                        "Match": r.get("match_score", 0),
                        "Status": display_status(r),
                        "Location": r.get("location"),
                        "Reason": r.get("reason"),
                    }
                    for r in filtered
                ]),
                hide_index=True,
                use_container_width=True,
                column_config={"Match": st.column_config.ProgressColumn("Match", min_value=0, max_value=100, format="%d%%")},
            )

            st.caption("PDF checks are shown only after an explicit check. Review AI-generated content before applying.")

            for idx, r in enumerate(filtered):
                with st.expander(f"{r.get('company')} · {r.get('title')} · {r.get('match_score', 0)}%"):
                    st.text(display_status(r))
                    st.write(r.get("reason", "No match reason recorded."))
                    st.caption("Missing skills: " + (", ".join(r.get("missing_skills", [])) or "None recorded"))

                    raw_url = str(r.get("url", ""))
                    if is_safe_url(raw_url):
                        st.link_button("View job posting", raw_url)

                    # Side-by-Side Review: Original JD vs Tailored Resume Content
                    c_jd, c_tailor = st.columns(2, gap="medium")
                    with c_jd:
                        st.markdown('<div class="review-panel-title">Original Job Description</div>', unsafe_allow_html=True)
                        st.text_area(
                            "Original JD",
                            value=r.get("jd_text", "No description text provided."),
                            height=200,
                            disabled=True,
                            label_visibility="collapsed",
                            key=f"jd_review_{idx}_{r.get('job_id', idx)}",
                        )
                    with c_tailor:
                        st.markdown('<div class="review-panel-title">AI Tailored Resume Content</div>', unsafe_allow_html=True)
                        tailored_data = r.get("tailored_resume")
                        if tailored_data:
                            sum_text = tailored_data.get("summary", "Summary tailored to role requirements.")
                            st.markdown(f"**Targeted Summary:**\n\n{sum_text}")
                            skills_arr = tailored_data.get("skills", [])
                            if skills_arr:
                                st.caption("Prioritized Skills: " + ", ".join(skills_arr[:8]))
                        elif r.get("pdf_path"):
                            st.info("Verified single-page resume generated. Download or inspect checks below.")
                        else:
                            st.caption(f"No resume generated for this job ({display_status(r)}).")

                    # Actual PDF ATS Audit Checklist & Download
                    p_path = r.get("pdf_path")
                    if p_path and Path(p_path).is_file():
                        file = Path(p_path)
                        check_id = f"{r.get('job_id', idx)}:{file.stat().st_mtime_ns}"
                        checks = st.session_state.setdefault("pdf_checks", {})
                        dcol, acol = st.columns([2, 1])
                        with dcol:
                            st.download_button(
                                "Download PDF",
                                file.read_bytes(),
                                file_name=file.name,
                                mime="application/pdf",
                                key=f"pdf_{idx}_{r.get('job_id')}",
                                use_container_width=True,
                            )
                        with acol:
                            if st.button("Check PDF", key=f"audit_{idx}_{r.get('job_id')}", use_container_width=True):
                                try:
                                    checks[check_id] = audit_resume_pdf(str(file))
                                except Exception as exc:
                                    checks[check_id] = {"passed": False, "errors": [str(exc)]}

                        if check_id in checks:
                            check = checks[check_id]
                            st.markdown(render_ats_checklist(check), unsafe_allow_html=True)
                            if check["passed"]:
                                st.success(f"Existing PDF checks passed · {check.get('details', {}).get('page_count', '?')} page(s).")
                            else:
                                st.warning("PDF checks flagged: " + "; ".join(check.get("errors", [])))
                            for warning in check.get("warnings", []):
                                st.caption(warning)
                        else:
                            st.caption("PDF generated · check results not recorded in this view.")
                    elif p_path:
                        st.warning("This PDF is no longer available. It may have expired or been deleted.")
    else:
        st.markdown(
            '<div class="empty-state">Your results will appear here.<br>Upload a jobs CSV and start a batch to get going.</div>',
            unsafe_allow_html=True,
        )
