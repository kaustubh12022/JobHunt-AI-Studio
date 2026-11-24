"""JobHunt AI Studio: Kaustubh Kale's private, locked resume studio."""
from __future__ import annotations

import logging
import time
import uuid
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from core.access import access_configured, verify_access
from core.ats_verifier import audit_resume_pdf
from core.batch_runner import batch_runner
from core.cost_estimator import estimate_batch_cost
from core.csv_parser import generate_sample_csv_str, parse_jobs_csv
from core.health import perform_system_health_check
from core.models import JobInput, RunConfig, RunStatus
from core.profile_manager import load_owner_profile, profile_to_yaml_str
from core.resume_review import REVIEW_MODEL, approved_pdf, approved_pdf_bytes
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
    SCORE_THRESHOLD,
    deepseek_api_key,
)
from ui import (
    candidate_card,
    counts,
    display_status,
    filter_results,
    metric_strip,
    pipeline_html,
    render_ats_checklist,
    render_health_card,
    render_live_log_console,
    section,
    styles,
)

logger = logging.getLogger("JobHuntAIStudio")

st.set_page_config(
    page_title="JobHunt AI Studio",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="auto",
)
styles()

# ==========================================
# OWNER GATE (fails closed; nothing private renders before unlock)
# ==========================================
if not access_configured():
    st.markdown('<span class="eyebrow">PRIVATE STUDIO</span>', unsafe_allow_html=True)
    st.title("Studio locked")
    st.error(
        "STUDIO_ACCESS_KEY is not configured on this server. "
        "Set a passphrase of at least 12 characters and restart the studio."
    )
    st.stop()

if not st.session_state.get("owner_unlocked", False):
    st.markdown('<span class="eyebrow">PRIVATE STUDIO</span>', unsafe_allow_html=True)
    st.title("Owner access only")
    st.caption("This is a private studio. Enter the owner passphrase to continue.")
    passphrase = st.text_input("Owner passphrase", type="password", key="owner_passphrase")
    failed = st.session_state.get("owner_failed_attempts", 0)
    if st.button("Unlock studio", type="primary", use_container_width=True):
        if verify_access(passphrase):
            st.session_state["owner_unlocked"] = True
            st.session_state["owner_failed_attempts"] = 0
            st.rerun()
        else:
            st.session_state["owner_failed_attempts"] = failed + 1
            time.sleep(min(2.0, 0.25 * (failed + 1)))
            st.error("Incorrect passphrase.")
    if failed >= 3:
        st.caption("Repeated failures are rate-limited.")
    st.stop()

# ==========================================
# LOCKED SESSION SETUP
# ==========================================
st.session_state.setdefault("session_id", uuid.uuid4().hex[:12])
session_id = st.session_state.session_id

try:
    session_manager.cleanup_expired_sessions()
except Exception:
    pass

# The profile is always reloaded from the bundled owner file: it cannot be edited.
st.session_state.profile = load_owner_profile()
for key in ("batch_results", "zip_bytes"):
    st.session_state.setdefault(key, None)

run_id = st.session_state.get("current_run_id")
recovery_code = st.session_state.get("current_recovery_code")
active_run_state = run_service.get_run_status(run_id, recovery_code) if run_id else {}
authorized = active_run_state.get("is_authorized", False)
run_config = active_run_state.get("config", {})
review_required = bool(run_config.get("reviewer_model", True))
events = active_run_state.get("events", [])
is_running = bool(run_id and authorized and (
    batch_runner.is_running(run_id) or active_run_state.get("status") == RunStatus.RUNNING.value
))
if authorized and not is_running:
    st.session_state.batch_results = active_run_state["completed_jobs"] or None
    if st.session_state.batch_results:
        for result in st.session_state.batch_results:
            if review_required and result["review_status"] == "Approved" and not approved_pdf(result):
                result["status"] = result["review_status"] = "Artifact verification failed"
                result["pdf_path"] = None
        artifact = run_service.package_run_artifacts(run_id, recovery_code, persist=False)
        st.session_state.zip_bytes = artifact.zip_bytes
        st.session_state.summary_csv = artifact.summary_csv_str
        st.session_state.summary_html = artifact.summary_html_str
    if active_run_state["status"] == RunStatus.COMPLETED.value:
        st.session_state.pipeline_progress = 1.0
        st.session_state.pipeline_view = pipeline_html(
            results=st.session_state.batch_results or [], total=active_run_state["total_jobs"], complete=True,
            company="Batch complete",
            role="Open Review & download below to inspect the approved resumes.",
        )

server_key_ready = bool(deepseek_api_key())


def clear_results() -> None:
    """Resets results, artifacts, and search state for the current session."""
    for key in (
        "batch_results",
        "zip_bytes",
        "summary_csv",
        "summary_html",
        "pipeline_view",
        "pipeline_progress",
        "pdf_checks",
        "result_query",
        "result_group",
        "current_run_id",
        "current_recovery_code",
    ):
        st.session_state.pop(key, None)


# ==========================================
# SIDEBAR: Owner, health, recovery, session
# ==========================================
with st.sidebar:
    st.markdown('<div class="brand"><span class="brand-mark">JH</span> JobHunt AI Studio</div>', unsafe_allow_html=True)
    st.caption(f"Private studio · {OWNER_NAME}")
    st.divider()

    try:
        health_info = perform_system_health_check()
        st.markdown(render_health_card(health_info), unsafe_allow_html=True)
    except Exception:
        pass
    st.divider()

    st.markdown("##### Locked engine")
    if is_running:
        st.markdown('<div class="locked-badge">&#128274; Batch running · settings locked</div>', unsafe_allow_html=True)
    st.caption(f"{MODEL_LABEL} · `{MODEL}`")
    st.caption(f"Owner profile: {OWNER_NAME}")
    if server_key_ready:
        st.caption("DeepSeek credential: configured on this server")
    else:
        st.warning("DeepSeek credential missing on this server. Set DEEPSEEK_API_KEY.")
    st.caption("No visitor can change the profile, model, keys or batch policy.")
    st.divider()

    with st.expander("Run recovery & lookup", expanded=False):
        st.caption("Inspect, resume, or download previous batch runs using a 12-character recovery code.")
        lookup_code = st.text_input("Batch recovery code", placeholder="jh-xxxx-xxxx-xxxx", key="sidebar_recovery_code")
        if st.button("Inspect run", key="btn_inspect_run", use_container_width=True, disabled=is_running) and lookup_code:
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
            inspected = st.session_state["inspected_run"]
            irun = run_store_instance.get_run(inspected["run_id"]) or inspected
            icode = st.session_state["inspected_code"]
            st.text(f"Run {irun['run_id']} · {irun['status']} · {irun['completed_count']}/{irun['total_jobs']} scored")
            if st.button("Load results into workspace", key="btn_load_inspected", use_container_width=True, disabled=is_running):
                clear_results()
                st.session_state["current_run_id"] = irun["run_id"]
                st.session_state["current_recovery_code"] = icode
                st.rerun()
            if st.button("Delete run & artifacts", key="btn_delete_inspected", use_container_width=True, disabled=is_running):
                try:
                    run_service.delete_run(irun["run_id"], icode)
                    st.session_state.pop("inspected_run", None)
                    if irun["run_id"] == run_id:
                        clear_results()
                    st.rerun()
                except (RuntimeError, PermissionError) as error:
                    st.error(str(error))

    with st.expander("Session & storage"):
        st.caption(f"Session: {session_id}")
        st.caption(f"Contact on file: {OWNER_EMAIL}")
        st.caption("Local workspace files expire automatically.")
        if st.button("Delete session files", use_container_width=True, disabled=is_running):
            session_manager.cleanup_session(session_id)
            session_manager.clear_checkpoint(session_id)
            clear_results()
            st.rerun()
        if st.button("Lock studio", use_container_width=True):
            st.session_state.clear()
            st.rerun()

# ==========================================
# MAIN WORKSPACE HEADER & TABS
# ==========================================
st.markdown('<span class="eyebrow">YOUR NEXT CHAPTER, PREPARED.</span>', unsafe_allow_html=True)
st.title("Make every application count.")
st.markdown(
    f'<p class="hero-copy">Kaustubh\'s private studio. Match your experience to the right roles and '
    f'create verified single-page tailored resumes in one focused workflow.</p>',
    unsafe_allow_html=True,
)
tab_batch, tab_profile, tab_guide = st.tabs(["Workspace", "Master profile", "Guide & templates"])

# ------------------------------------------
# TAB: MASTER PROFILE (read-only, locked)
# ------------------------------------------
with tab_profile:
    section("01", "Kaustubh's master profile", "Locked source of truth for every score and every tailored resume.")
    candidate_card(st.session_state.profile)
    profile = st.session_state.profile
    info = profile.get("personal_information", {})

    contact_col, export_col = st.columns([2, 1], gap="large")
    with contact_col:
        with st.container(border=True):
            st.markdown("##### Contact")
            st.text(f"Name: {info.get('name')} {info.get('surname')}")
            st.text(f"Email: {info.get('email')}")
            st.text(f"Phone: {info.get('phone')}")
            st.text(f"Location: {info.get('address')}")
            st.text(f"LinkedIn: {info.get('linkedin')}")
            st.text(f"GitHub: {info.get('github')}")
            st.caption("Target roles: " + ", ".join(profile.get("target_roles", [])))
    with export_col:
        with st.container(border=True):
            st.markdown("##### Locked profile")
            st.caption("Editing and uploading are disabled by design. To change the profile, edit data/profile.yaml in the repository.")
            st.download_button(
                "Download profile (.yaml)",
                profile_to_yaml_str(profile),
                file_name="kaustubh_kale_master_profile.yaml",
                mime="text/yaml",
                use_container_width=True,
                key="tab2_dl_active_yaml",
            )

    with st.expander("Professional summary", expanded=True):
        st.write(profile.get("profile_summary", "No summary recorded."))

    with st.expander("Education", expanded=True):
        for edu in profile.get("education_details", []):
            st.markdown(
                f"**{edu.get('education_level')}** · {edu.get('institution')}  \n"
                f"{edu.get('dates')} · {edu.get('location')}  \n"
                f"{edu.get('final_evaluation_grade')}"
            )
            for line in edu.get("secondary_education", []) or []:
                st.caption(line)

    with st.expander("Experience", expanded=True):
        for exp in profile.get("experience_details", []):
            st.markdown(
                f"**{exp.get('position')}** · {exp.get('company')}  \n"
                f"{exp.get('dates')} · {exp.get('location')}"
            )
            for bullet in exp.get("key_responsibilities", []):
                st.markdown(f"- {bullet}")

    with st.expander(f"Projects · {len(profile.get('projects', []))}", expanded=True):
        for proj in profile.get("projects", []):
            st.markdown(f"**{proj.get('name')}** · {proj.get('tech_stack')}")
            for bullet in proj.get("description_bullets", []):
                st.markdown(f"- {bullet}")
            if proj.get("link"):
                st.caption(proj["link"])
            st.divider()

    with st.expander(f"Skills · {len(profile.get('skills', []))}", expanded=True):
        structured = profile.get("skills_structured", {})
        if structured:
            for group, group_skills in structured.items():
                st.markdown(f"**{group}**")
                st.markdown(" · ".join(group_skills))
        with st.expander("Full skill inventory"):
            st.markdown(" · ".join(profile.get("skills", [])))

    with st.expander("Certifications, achievements & languages"):
        for cert in profile.get("certifications", []):
            if isinstance(cert, dict):
                st.markdown(f"**{cert.get('name')}** · {cert.get('description', '')}")
            else:
                st.markdown(f"- {cert}")
        for ach in profile.get("achievements", []):
            if isinstance(ach, dict):
                st.markdown(f"**{ach.get('name')}** · {ach.get('description', '')}")
            else:
                st.markdown(f"- {ach}")
        for lang in profile.get("languages", []) or []:
            st.caption(f"{lang.get('language')} · {lang.get('proficiency')}")
        prefs = profile.get("work_preferences", {}) or {}
        if prefs:
            st.caption(" · ".join(f"{k}: {v}" for k, v in prefs.items()))

# ------------------------------------------
# TAB: GUIDE & TEMPLATES
# ------------------------------------------
with tab_guide:
    section("01", "Start with the right inputs", "Upload a jobs CSV. The locked profile and DeepSeek engine do the rest.")
    st.download_button(
        "Download sample jobs CSV",
        generate_sample_csv_str(),
        file_name="sample_jobs_template.csv",
        mime="text/csv",
        type="primary",
        use_container_width=True,
    )
    st.dataframe(
        pd.DataFrame([
            {"Field": "Company", "Required": "Yes", "Accepted headers": "Company, employer, organization"},
            {"Field": "Job title", "Required": "Yes", "Accepted headers": "Title, job title, role, position"},
            {"Field": "Description", "Required": "Yes", "Accepted headers": "Job description, jd_text, description, content"},
            {"Field": "Apply URL", "Required": "No", "Accepted headers": "URL, job link, apply_url"},
            {"Field": "Official company website", "Required": "No", "Accepted headers": "company_website, official_website"},
            {"Field": "Location", "Required": "No", "Accepted headers": "Location, city, work_mode"},
        ]),
        hide_index=True,
        use_container_width=True,
    )
    with st.expander("How the pipeline works", expanded=True):
        st.markdown(
            "1. Validate the CSV and score every job with DeepSeek Flash.\n"
            "2. Check available job and company sources for shortlisted roles.\n"
            f"3. Draft a resume for jobs at or above {SCORE_THRESHOLD}% match (up to {MAX_RESUMES} per batch).\n"
            "4. Render a single-page A4 PDF and verify the layout locally.\n"
            f"5. Send the PDF to DeepSeek Flash for an independent review at {APPROVAL_THRESHOLD}/100.\n"
            f"6. Revise based on the findings for up to {MAX_DRAFTS} drafts.\n"
            "7. Package approved resumes with the results reports."
        )
    with st.expander("Quotas, recovery & reviewing outputs"):
        st.write("Every batch is checkpointed in SQLite. An interrupted run can be resumed with its recovery code, "
                 "and scoring tokens are never spent twice.")
        st.write("Always review tailored content before submitting applications. Single-page PDFs undergo automated "
                 "verification against ATS layout standards.")

# ------------------------------------------
# TAB: WORKSPACE (Setup -> Live Pipeline -> Results)
# ------------------------------------------
with tab_batch:
    section("01", "Set up your batch", "Upload the jobs CSV and start the locked pipeline.")
    candidate_card(st.session_state.profile)

    upload_col, policy_col = st.columns([1.7, 1], gap="large")
    parsed_jobs = None

    with upload_col:
        uploaded_file = st.file_uploader(
            "Jobs CSV",
            type=["csv"],
            disabled=is_running,
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

    with policy_col:
        with st.container(border=True):
            st.markdown("##### Locked policy")
            if is_running:
                st.markdown('<div class="locked-badge">&#128274; Configuration locked</div>', unsafe_allow_html=True)
            st.markdown(
                f"- Engine: **{MODEL_LABEL}** (`{MODEL}`)\n"
                f"- Match threshold: **{SCORE_THRESHOLD}%**\n"
                f"- Resume cap: **{MAX_RESUMES}**\n"
                f"- Challenger approval: **{APPROVAL_THRESHOLD}/100**\n"
                f"- Draft limit: **{MAX_DRAFTS}**\n"
                f"- Candidate: **{OWNER_NAME}**"
            )
            if not server_key_ready:
                st.error("Server is missing DEEPSEEK_API_KEY.")

    if parsed_jobs:
        cost = estimate_batch_cost(
            total_jobs=len(parsed_jobs),
            max_resumes=MAX_RESUMES,
            expected_shortlist_rate=0.50,
        )
        budget_label = escape(cost["cost_display"])
        st.markdown(
            f'<div class="estimate">'
            f'<strong>Estimated load · ~{cost["total_tokens"]:,} tokens</strong>'
            f'<span>{budget_label} · ~{cost["estimated_tailored"]} tailored resumes</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"Up to {min(len(parsed_jobs), MAX_RESUMES) * MAX_DRAFTS} drafts and DeepSeek reviews. "
                   "Revisions and source checks can increase token usage.")

    can_run = bool(parsed_jobs and server_key_ready)
    missing = []
    if not parsed_jobs:
        missing.append("upload a valid jobs CSV")
    if not server_key_ready:
        missing.append("configure DEEPSEEK_API_KEY on the server")
    st.caption("To start, " + " and ".join(missing) + "." if missing else "Ready to score, tailor, and review.")

    actions = st.columns([2, 1])
    with actions[0]:
        if st.button("Start batch", type="primary", use_container_width=True, disabled=not can_run or is_running):
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
                new_id, code = run_service.create_run(
                    config, st.session_state.profile, [JobInput.from_dict(job) for job in parsed_jobs],
                )
            except (RuntimeError, ValueError) as error:
                st.error(str(error))
            else:
                clear_results()
                st.session_state.current_run_id = new_id
                st.session_state.current_recovery_code = code
                batch_runner.start_batch(new_id, code)
                st.rerun()
    with actions[1]:
        if st.button("Clear results", use_container_width=True, disabled=is_running):
            clear_results()
            st.rerun()

    section("02", "Live pipeline", "Follow job scoring, PDF rendering, and challenger decisions.")
    if authorized:
        with st.expander("Recovery code"):
            st.code(recovery_code, language=None)
        start_error = batch_runner.get_error(run_id)
        if start_error:
            st.error(start_error)
        status = active_run_state["status"]
        if not is_running and status in ("paused", "awaiting_credentials", "failed", "pending"):
            message = events[0]["message"] if events else status
            if status == "failed":
                st.error(message)
            else:
                st.warning(message)
            if st.button("Resume saved batch", disabled=not server_key_ready):
                batch_runner.start_batch(run_id, recovery_code)
                st.rerun()

        if is_running or status == "failed":
            current = events[0] if events else {}
            st.session_state.pipeline_view = pipeline_html(
                stage="Score" if status == "failed" else current.get("stage", "Validate"),
                company=current.get("message", "Starting background batch"), role="",
                results=active_run_state["completed_jobs"], total=active_run_state["total_jobs"],
                failed=status == "failed",
            )
    st.markdown(st.session_state.get("pipeline_view", pipeline_html(total=len(parsed_jobs or []))), unsafe_allow_html=True)

    if authorized and is_running:
        total = active_run_state["total_jobs"]
        processed = active_run_state["completed_count"]
        st.progress(min(1.0, processed / max(1, total)), text=f"{processed}/{total} jobs scored")
        if st.button("Pause background batch", key="btn_stop_batch", use_container_width=True):
            run_service.pause_run(run_id, recovery_code)
            st.info("Pause requested. In-flight stages will save their progress.")
        if events:
            st.markdown(render_live_log_console([event["message"] for event in reversed(events)]), unsafe_allow_html=True)
        time.sleep(1)
        st.rerun()

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
                ("Approved PDFs" if review_required else "Unreviewed PDFs", c["generated"]),
                ("Average match", f"{avg_score}%"),
                ("Needs attention", c["attention"]),
            )),
            unsafe_allow_html=True,
        )

        usage = active_run_state.get("usage", {})
        if usage:
            st.caption(f"Recorded tokens across scoring, drafts, and reviews · {usage.get('total_tokens', 0):,}")
        if c["attention"]:
            st.warning(f"{c['attention']} job(s) require attention.")
            retryable = any("error" in r["status"].lower() or r["scoring_status"] == "failed" for r in results)
            if authorized and retryable and st.button(
                "Retry failed stages", disabled=not server_key_ready,
            ):
                batch_runner.start_batch(run_id, recovery_code)
                st.rerun()

        download_cols = st.columns([2, 1, 1])
        with download_cols[0]:
            if st.session_state.zip_bytes:
                st.download_button(
                    "Download approved ZIP" if review_required else "Download unreviewed ZIP",
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
        with download_cols[2]:
            s_html = st.session_state.summary_html
            st.download_button(
                "Download HTML Report",
                s_html,
                file_name="shortlisted_jobs_report.html",
                mime="text/html",
                use_container_width=True,
            )

        search_col, filter_col = st.columns([2, 1])
        with search_col:
            query = st.text_input("Search jobs", placeholder="Company, role or location", key="result_query")
        with filter_col:
            group = st.selectbox(
                "Show",
                ["All jobs", "Approved", "Below Threshold", "Failed", "Skipped", "Needs attention"],
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
                        "Challenger": r.get("challenger_score"),
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

            st.caption("Review the PDF and the challenger findings before applying.")

            for idx, r in enumerate(filtered):
                with st.expander(f"{r.get('company')} · {r.get('title')} · {r.get('match_score', 0)}%"):
                    st.text(display_status(r))
                    st.write(r.get("reason", "No match reason recorded."))
                    st.caption("Missing skills: " + (", ".join(r.get("missing_skills", [])) or "None recorded"))

                    raw_url = str(r.get("url", ""))
                    if is_safe_url(raw_url):
                        st.link_button("View job posting", raw_url)

                    context = r.get("company_context") or {}
                    for warning in context.get("warnings", []):
                        st.caption(warning)
                    for i, source in enumerate(context.get("sources", []), start=1):
                        if is_safe_url(source["url"]):
                            st.link_button(f"View {source['kind']} source {i}", source["url"])
                    for attempt in r.get("attempts", []):
                        review = attempt.get("review")
                        if review:
                            verdict = "approved" if review["approved"] else "needs changes"
                            st.text(f"Draft {attempt['number']} · {review['score']:.1f}/100 · {verdict}")
                            for finding in review["findings"]:
                                st.text(f"{finding['section']} · {finding['evidence']} · {finding['correction']}")
                        for error in (attempt.get("audit") or {}).get("errors", []):
                            st.text(error)
                    if r.get("error"):
                        st.caption(r["error"])

                    # Side-by-side review
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
                            sum_text = tailored_data.get("profile_summary", "Summary tailored to role requirements.")
                            st.text(sum_text)
                            skills_arr = tailored_data.get("skills", [])
                            if skills_arr:
                                st.caption("Prioritized Skills: " + ", ".join(skills_arr[:8]))
                        elif r.get("pdf_path"):
                            st.info("A resume PDF is available below.")
                        else:
                            st.caption(f"No resume generated for this job ({display_status(r)}).")

                    # Actual PDF ATS Audit Checklist & Download
                    approved_content = approved_pdf_bytes(r) if review_required else None
                    is_approved = approved_content is not None
                    p_path = r.get("pdf_path") if is_approved or not review_required else r.get("draft_path")
                    if p_path and Path(p_path).is_file():
                        file = Path(p_path)
                        check_id = f"{r.get('job_id', idx)}:{file.stat().st_mtime_ns}"
                        checks = st.session_state.setdefault("pdf_checks", {})
                        dcol, acol = st.columns([2, 1])
                        with dcol:
                            st.download_button(
                                "Download approved PDF" if is_approved else "Download draft PDF",
                                approved_content if is_approved else file.read_bytes(),
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
        st.markdown("Your results will appear here after the batch processes jobs.")

    if authorized and not is_running:
        with st.expander("Run management"):
            if st.button("Delete run & purge artifacts", key="btn_purge_active_run"):
                try:
                    run_service.delete_run(run_id, recovery_code)
                except (RuntimeError, PermissionError) as error:
                    st.error(str(error))
                else:
                    clear_results()
                    st.rerun()
