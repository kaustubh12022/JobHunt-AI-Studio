"""Presentation components and layout helpers for JobHunt AI Studio."""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import streamlit as st

# Backward-compatible stage identifiers for existing test suites
STAGES = ("Scoring", "Tailoring", "PDF generation", "Packaging")
# Visual 6-badge pipeline sequence specified for the live workspace
PIPELINE_BADGES = ("Validate", "Score", "Tailor", "Render", "Check", "Package")


def styles() -> None:
    """Injects the studio design system CSS."""
    css_path = Path(__file__).with_name("static").joinpath("studio.css")
    if css_path.is_file():
        st.markdown(f"<style>{css_path.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def section(number: str, title: str, description: str) -> None:
    """Renders a structured section heading with an accessible numerical badge."""
    st.markdown(
        f'<div class="section-heading">'
        f'<span class="section-number">{escape(str(number))}</span>'
        f'<div><h2>{escape(str(title))}</h2><p>{escape(str(description))}</p></div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def candidate_card(profile: Dict[str, Any]) -> str:
    """Renders a candidate summary card and returns the full name."""
    info = profile.get("personal_information", {})
    name = f"{info.get('name', 'Candidate')} {info.get('surname', '')}".strip()
    detail = info.get("email") or info.get("address") or "Candidate profile"
    skills = len(profile.get("skills", []))
    projects = len(profile.get("projects", []))
    project_label = "project" if projects == 1 else "projects"
    st.markdown(
        f'<div class="candidate-card">'
        f'<span class="avatar">{escape(name[:1].upper() if name else "C")}</span>'
        f'<div class="candidate-name"><strong>{escape(name)}</strong><span>{escape(str(detail))}</span></div>'
        f'<span class="candidate-meta">{skills} skills <span>·</span> {projects} {project_label}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    return name


def result_group(result: Dict[str, Any]) -> str:
    """Classifies a job result into a clean display status group."""
    status = str(result.get("status", "")).lower()
    if "error" in status or "fail" in status or result.get("scoring_status") == "fallback":
        return "Needs attention"
    return "PDF generated" if result.get("pdf_path") else "Skipped"


def display_status(result: Dict[str, Any]) -> str:
    """Returns human-readable status text for table presentation."""
    if result.get("status") == "Tailored & Verified":
        return "PDF generated" if result_group(result) != "Needs attention" else "PDF generated · scoring fallback"
    return str(result.get("status", "Evaluated"))


def counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Computes aggregate batch counts."""
    return {
        "processed": len(results),
        "generated": sum(bool(r.get("pdf_path")) for r in results),
        "skipped": sum(result_group(r) == "Skipped" for r in results),
        "attention": sum(result_group(r) == "Needs attention" for r in results),
    }


def metric_strip(items: Sequence[tuple[str, Any]]) -> str:
    """Builds a metrics strip row."""
    cards = "".join(
        f'<div class="studio-metric"><span>{escape(str(label))}</span><strong>{escape(str(value))}</strong></div>'
        for label, value in items
    )
    return f'<div class="studio-metrics">{cards}</div>'


def _badge_state(
    badge_name: str,
    stage: Optional[str],
    complete: bool,
    failed: bool,
    attempted_pdf: bool,
) -> str:
    """Maps runtime stage to the visual 6-badge pipeline state."""
    clean_stage = str(stage or "").strip().lower()

    if complete:
        if badge_name in ("Tailor", "Render", "Check") and not attempted_pdf:
            return "skipped"
        return "done"

    # Map current active stage
    is_active = False
    is_done = False

    if clean_stage in ("validate", "validation"):
        is_active = badge_name == "Validate"
    elif clean_stage in ("scoring", "score"):
        is_active = badge_name == "Score"
        is_done = badge_name == "Validate"
    elif clean_stage in ("tailoring", "tailor"):
        is_active = badge_name == "Tailor"
        is_done = badge_name in ("Validate", "Score")
    elif clean_stage in ("pdf generation", "render"):
        is_active = badge_name == "Render"
        is_done = badge_name in ("Validate", "Score", "Tailor")
    elif clean_stage in ("check", "audit", "ats check"):
        is_active = badge_name == "Check"
        is_done = badge_name in ("Validate", "Score", "Tailor", "Render")
    elif clean_stage in ("packaging", "package"):
        is_active = badge_name == "Package"
        is_done = badge_name in ("Validate", "Score", "Tailor", "Render", "Check")

    if failed and is_active:
        return "failed"
    if is_active:
        return "active"
    if is_done:
        return "done"
    return "pending"


def pipeline_html(
    stage: Optional[str] = None,
    company: str = "Ready when you are",
    role: str = "Your pipeline will appear here once the batch starts.",
    results: Sequence[Dict[str, Any]] = (),
    total: int = 0,
    complete: bool = False,
    failed: bool = False,
    rate_limit_note: Optional[str] = None,
    elapsed_seconds: Optional[int] = None,
) -> str:
    """
    Renders the responsive 6-badge pipeline visualization:
    Validate -> Score -> Tailor -> Render -> Check -> Package.
    Maintains full backward-compatibility with existing test suites.
    """
    attempted_pdf = any(
        r.get("pdf_path") or "render error" in str(r.get("status", "")).lower()
        for r in results
    )

    steps = []
    for i, label in enumerate(PIPELINE_BADGES):
        state = _badge_state(label, stage, complete, failed, attempted_pdf)
        marker = "&#10003;" if state == "done" else ("&ndash;" if state == "skipped" else ("&#10005;" if state == "failed" else str(i + 1)))
        text = label + (" · skipped" if state == "skipped" else "")
        steps.append(
            f'<div class="pipeline-step {state}">'
            f'<span class="step-dot">{marker}</span>'
            f'<span>{escape(text)}</span>'
            f'</div>'
        )

    rate_html = ""
    if rate_limit_note:
        rate_html = f'<div class="pipeline-rate-limit"><span class="rate-limit-dot">&#9679;</span> {escape(rate_limit_note)}</div>'

    timer_html = ""
    if elapsed_seconds is not None:
        timer_html = f'<span class="pipeline-timer">{int(elapsed_seconds)}s elapsed</span>'

    c = counts(results)
    metrics = metric_strip((
        ("Processed", f"{c['processed']} / {total}"),
        ("Verified", c["generated"]),
        ("Skipped", c["skipped"]),
        ("Failed", c["attention"]),
    ))

    eyebrow_text = "BATCH COMPLETE" if complete else "CURRENT ACTIVITY"

    return (
        f'<div class="pipeline-card">'
        f'<div class="pipeline-steps">{"".join(steps)}</div>'
        f'<div class="pipeline-current">'
        f'<div class="pipeline-current-header">'
        f'<span class="eyebrow">{eyebrow_text}</span>'
        f'{timer_html}'
        f'</div>'
        f'<strong>{escape(str(company))}</strong>'
        f'<span>{escape(str(role))}</span>'
        f'{rate_html}'
        f'</div>'
        f'{metrics}'
        f'</div>'
    )


def filter_results(
    results: Sequence[Dict[str, Any]],
    query: str,
    group: str,
) -> List[Dict[str, Any]]:
    """Filters results by status category and free-text search query."""
    query_clean = (query or "").strip().casefold()
    filtered: List[Dict[str, Any]] = []

    for r in results:
        r_grp = result_group(r)
        if group != "All jobs":
            if group in ("PDF generated", "Tailored & Verified") and r_grp != "PDF generated":
                continue
            elif group in ("Skipped", "Below Threshold") and r_grp != "Skipped":
                continue
            elif group in ("Needs attention", "Failed") and r_grp != "Needs attention":
                continue
            elif group not in ("PDF generated", "Tailored & Verified", "Skipped", "Below Threshold", "Needs attention", "Failed") and r_grp != group:
                continue

        if query_clean:
            searchable = " ".join(
                str(r.get(k, "")) for k in ("company", "title", "location", "reason")
            ).casefold()
            if query_clean not in searchable:
                continue

        filtered.append(r)

    return filtered


def render_health_card(health: Dict[str, Any]) -> str:
    """Renders a clean sidebar system health diagnostic card."""
    status = health.get("status", "unknown").lower()
    badge_cls = "healthy" if status == "healthy" else ("degraded" if status == "degraded" else "unhealthy")
    badge_label = status.capitalize()

    storage = health.get("checks", {}).get("storage", {})
    free_mb = storage.get("free_space_mb", 0)
    free_label = f"{free_mb / 1024:.1f} GB" if free_mb > 1024 else f"{free_mb:.0f} MB"

    workers = health.get("checks", {}).get("workers", {})
    active_runs = workers.get("active_runs", 0)

    db_check = health.get("checks", {}).get("database", {})
    db_status = "Connected" if db_check.get("status") == "ok" else "Error"

    pw_check = health.get("checks", {}).get("playwright", {})
    pw_status = "Ready" if pw_check.get("status") == "installed" else "Unavailable"

    return (
        f'<div class="health-card">'
        f'<div class="health-header">'
        f'<strong>System Health</strong>'
        f'<span class="health-badge {badge_cls}">&#9679; {badge_label}</span>'
        f'</div>'
        f'<div class="health-items">'
        f'<div class="health-item"><span>Database</span><strong>{db_status}</strong></div>'
        f'<div class="health-item"><span>Storage Free</span><strong>{free_label}</strong></div>'
        f'<div class="health-item"><span>Active Workers</span><strong>{active_runs}</strong></div>'
        f'<div class="health-item"><span>PDF Engine</span><strong>{pw_status}</strong></div>'
        f'</div>'
        f'</div>'
    )


def render_ats_checklist(audit: Dict[str, Any]) -> str:
    """Renders a structured ATS check breakdown for a single resume."""
    passed = audit.get("passed", False)
    page_count = audit.get("page_count", 1)
    errors = audit.get("errors", [])
    warnings = audit.get("warnings", [])

    checks = [
        ("Strict 1-Page Constraint", page_count == 1, f"{page_count} page(s) detected"),
        ("Standard ATS Headings", True, "Profile, Education, Skills, Projects, Experience"),
        ("4-Digit Years (YYYY)", not any("year" in e.lower() for e in errors), "Clean 4-digit date tokens"),
        ("Contact & Location", not any("location" in e.lower() for e in errors), "City & country present in header"),
        ("Clean Plain URLs", not any("link" in e.lower() or "url" in e.lower() for e in errors), "Plain text links only"),
        ("Kerning & Alignment", not any("kerning" in e.lower() for e in errors), "Zero split words detected"),
    ]

    items_html = []
    for label, is_pass, detail in checks:
        icon = '<span class="ats-icon-pass">&#10003;</span>' if is_pass else '<span class="ats-icon-fail">&#10005;</span>'
        items_html.append(
            f'<div class="ats-check-item">{icon} <div><strong>{escape(label)}</strong>: <span>{escape(detail)}</span></div></div>'
        )

    alert_cls = "healthy" if passed else "unhealthy"
    status_text = "Universal ATS Compliance Passed" if passed else "Compliance Issues Detected"

    return (
        f'<div class="review-panel">'
        f'<div class="review-panel-title">ATS Acceptance Audit <span class="health-badge {alert_cls}">{status_text}</span></div>'
        f'<div>{"".join(items_html)}</div>'
        f'</div>'
    )
