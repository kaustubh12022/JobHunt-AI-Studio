"""Small, escaped presentation helpers for the Streamlit workspace."""
from html import escape
from pathlib import Path

import streamlit as st

STAGES = ("Scoring", "Tailoring", "PDF generation", "Packaging")


def styles():
    st.markdown(f"<style>{Path(__file__).with_name('static').joinpath('studio.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def section(number, title, description):
    st.markdown(f'<div class="section-heading"><span class="section-number">{escape(number)}</span><div><h2>{escape(title)}</h2><p>{escape(description)}</p></div></div>', unsafe_allow_html=True)


def candidate_card(profile):
    info = profile.get("personal_information", {})
    name = f"{info.get('name', 'Candidate')} {info.get('surname', '')}".strip()
    detail = info.get("email") or info.get("address") or "Candidate profile"
    skills = len(profile.get("skills", []))
    projects = len(profile.get("projects", []))
    project_label = "project" if projects == 1 else "projects"
    st.markdown(f'<div class="candidate-card"><span class="avatar">{escape(name[:1].upper())}</span><div class="candidate-name"><strong>{escape(name)}</strong><span>{escape(str(detail))}</span></div><span class="candidate-meta">{skills} skills <span>·</span> {projects} {project_label}</span></div>', unsafe_allow_html=True)
    return name


def result_group(result):
    if "error" in str(result.get("status", "")).lower() or result.get("scoring_status") == "fallback":
        return "Needs attention"
    return "PDF generated" if result.get("pdf_path") else "Skipped"


def display_status(result):
    if result.get("status") == "Tailored & Verified":
        return "PDF generated" if result_group(result) != "Needs attention" else "PDF generated · scoring fallback"
    return str(result.get("status", "Evaluated"))


def counts(results):
    return {
        "processed": len(results),
        "generated": sum(bool(r.get("pdf_path")) for r in results),
        "skipped": sum(result_group(r) == "Skipped" for r in results),
        "attention": sum(result_group(r) == "Needs attention" for r in results),
    }


def metric_strip(items):
    cards = ''.join(f'<div class="studio-metric"><span>{escape(str(label))}</span><strong>{escape(str(value))}</strong></div>' for label, value in items)
    return f'<div class="studio-metrics">{cards}</div>'


def pipeline_html(stage=None, company="Ready when you are", role="Your pipeline will appear here once the batch starts.", results=(), total=0, complete=False, failed=False):
    active = STAGES.index(stage) if stage in STAGES else -1
    steps = []
    attempted_pdf = any(r.get("pdf_path") or "render error" in str(r.get("status", "")).lower() for r in results)
    for i, label in enumerate(STAGES):
        state = "done" if complete else ("active" if i == active else "pending")
        if complete and i in (1, 2) and not attempted_pdf:
            state = "skipped"
        # Previous steps refer to the current job, never to the whole batch.
        if active in (1, 2) and i < active:
            state = "done"
        if failed and i == active:
            state = "failed"
        marker = "✓" if state == "done" else ("–" if state == "skipped" else i + 1)
        text = label + (" · skipped" if state == "skipped" else "")
        steps.append(f'<div class="pipeline-step {state}"><span class="step-dot">{marker}</span><span>{text}</span></div>')
    c = counts(results)
    metrics = metric_strip((("Processed", f"{c['processed']} / {total}"), ("PDFs generated", c['generated']), ("Skipped", c['skipped']), ("Needs attention", c['attention'])))
    return f'<div class="pipeline-card"><div class="pipeline-steps">{"".join(steps)}</div><div class="pipeline-current"><span class="eyebrow">{"BATCH COMPLETE" if complete else "CURRENT ACTIVITY"}</span><strong>{escape(str(company))}</strong><span>{escape(str(role))}</span></div>{metrics}</div>'


def filter_results(results, query, group):
    query = query.strip().casefold()
    return [r for r in results if (group == "All jobs" or result_group(r) == group) and (not query or query in ' '.join(str(r.get(k, "")) for k in ("company", "title", "location")).casefold())]
