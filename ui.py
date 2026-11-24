"""Presentation helpers for the JobHunt AI Studio owner console.

The interface is deliberately plain: legible text, one primary action per
screen, stable labels, and a machine-readable status block. That keeps the app
usable both by a human and by an automated browser agent (Playwright / vision).
"""
from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import streamlit as st

# Fixed status vocabulary shared by the banner, the JSON snapshot and the docs.
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

STATUS_WORDS = {
    STATUS_READY: "READY",
    STATUS_RUNNING: "RUNNING",
    STATUS_PAUSED: "PAUSED",
    STATUS_COMPLETE: "COMPLETE",
    STATUS_FAILED: "FAILED",
}


def styles() -> None:
    """Injects the studio stylesheet."""
    css_path = Path(__file__).with_name("static").joinpath("studio.css")
    if css_path.is_file():
        st.markdown(f"<style>{css_path.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def status_banner(
    status: str,
    detail: str = "",
    *,
    totals: Optional[Dict[str, Any]] = None,
) -> str:
    """Renders the single status block agents read first.

    The visible word (READY / RUNNING / COMPLETE / ...) is repeated in the
    ``data-studio-state`` attribute, and the counters are exposed as
    ``data-*`` attributes, so a browser agent can read the state without
    screenshotting or guessing at layout.
    """
    word = STATUS_WORDS.get(status, STATUS_WORDS[STATUS_READY])
    counters = totals or {}
    attrs = {
        "data-studio-state": status,
        "data-jobs-total": counters.get("total", 0),
        "data-jobs-scored": counters.get("scored", 0),
        "data-approved": counters.get("approved", 0),
        "data-attention": counters.get("attention", 0),
        "data-zip": 1 if counters.get("zip") else 0,
        "data-run-id": counters.get("run_id", "") or "",
    }
    attr_html = " ".join(f'{name}="{escape(str(value))}"' for name, value in attrs.items())
    return (
        f'<div class="status-banner" {attr_html}>'
        f'<span class="status-label">STATUS</span>'
        f'<strong class="status-word">{escape(word)}</strong>'
        f'<span class="status-detail">{escape(str(detail))}</span>'
        f"</div>"
    )


def state_json(snapshot: Dict[str, Any]) -> str:
    """Serializes the studio state for the machine-readable panel."""
    return json.dumps(snapshot, indent=2, ensure_ascii=False, default=str)


def result_group(result: Dict[str, Any]) -> str:
    """Classifies a job result into a plain display group."""
    status = str(result.get("status", "")).lower()
    if (
        "error" in status
        or "fail" in status
        or result.get("review_status") == "Needs manual review"
        or result.get("scoring_status") in ("failed", "fallback")
    ):
        return "Needs attention"
    if result.get("review_status") == "Approved":
        return "Approved"
    return "PDF generated" if result.get("pdf_path") else "Skipped"


def display_status(result: Dict[str, Any]) -> str:
    """Returns the status text shown for one job."""
    if result.get("status") == "Tailored & Verified":
        return "Unreviewed PDF" if result_group(result) != "Needs attention" else "PDF generated · scoring fallback"
    return str(result.get("status", "Evaluated"))


def counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Computes aggregate batch counts."""
    return {
        "processed": len(results),
        "generated": sum(bool(r.get("pdf_path")) for r in results),
        "approved": sum(result_group(r) == "Approved" for r in results),
        "skipped": sum(result_group(r) == "Skipped" for r in results),
        "attention": sum(result_group(r) == "Needs attention" for r in results),
    }


def filter_results(results: Sequence[Dict[str, Any]], group: str = "All jobs") -> List[Dict[str, Any]]:
    """Filters results by display group."""
    if not group or group == "All jobs":
        return list(results)
    allowed = {
        "Approved": ("Approved",),
        "PDF generated": ("PDF generated", "Approved"),
        "Below Threshold": ("Skipped",),
        "Needs attention": ("Needs attention",),
    }.get(group, (group,))
    return [r for r in results if result_group(r) in allowed]


def table_html(frame: Any) -> str:
    """Renders a DataFrame as plain HTML so every cell stays readable to agents.

    Streamlit's interactive dataframe draws cells on a canvas, which browser
    automation cannot read. A simple table keeps the values in the DOM.
    """
    import pandas as pd

    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame(frame)
    return f'<div class="table-wrap">{frame.to_html(index=False, border=0, classes="data-table", escape=True)}</div>'


def render_live_log_console(logs: Sequence[str], max_lines: int = 10) -> str:
    """Renders the plain-text activity log shown while a batch runs."""
    recent = [str(line).strip() for line in list(logs)[-max_lines:]] if logs else ["Waiting for the first event."]
    rows = []
    for line in recent:
        lowered = line.lower()
        tone = "info"
        if "fail" in lowered or "error" in lowered or "rejected" in lowered:
            tone = "error"
        elif "complete" in lowered or "approved" in lowered or "packaged" in lowered:
            tone = "ok"
        elif "pause" in lowered or "waiting" in lowered or "retry" in lowered:
            tone = "warn"
        rows.append(f'<div class="log-line log-{tone}">{escape(line)}</div>')
    return (
        '<div class="log-card">'
        '<div class="log-title">Activity log</div>'
        f'<div class="log-body">{"".join(rows)}</div>'
        "</div>"
    )
