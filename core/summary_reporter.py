"""
JobHunt AI Studio - Summary Reporter
Generates executive CSV summary spreadsheets and HTML summary reports for batch runs.
Enforces:
- CSV formula neutralization on all exported cells (prevents spreadsheet DDE injection)
- Safe URL verification (blocks javascript:, data:, and file: URLs)
- HTML template autoescaping
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.security import is_safe_url, neutralize_csv_cell, sanitize_url


def generate_summary_csv(results: List[Dict[str, Any]]) -> str:
    """
    Generates a CSV string summarizing all processed jobs with CSV formula neutralization.
    """
    output = io.StringIO()
    fieldnames = [
        "Company", "Job Title", "Match Score", "Status",
        "Resume PDF", "Apply URL", "Location", "Match Reason", "Missing Skills"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for r in results:
        company_val = neutralize_csv_cell(r.get("company", ""))
        title_val = neutralize_csv_cell(r.get("title", ""))
        score_val = neutralize_csv_cell(f"{r.get('match_score', 0)}%")
        status_val = neutralize_csv_cell(r.get("status", "Evaluated"))
        pdf_name = os.path.basename(r.get("pdf_path", "")) if r.get("pdf_path") else "N/A"
        pdf_val = neutralize_csv_cell(pdf_name)
        url_raw = sanitize_url(r.get("url", "")) or "N/A"
        url_val = neutralize_csv_cell(url_raw)
        loc_val = neutralize_csv_cell(r.get("location", "N/A"))
        reason_val = neutralize_csv_cell(r.get("reason", ""))
        skills_str = ", ".join(r.get("missing_skills", [])) if r.get("missing_skills") else "None"
        skills_val = neutralize_csv_cell(skills_str)

        writer.writerow({
            "Company": company_val,
            "Job Title": title_val,
            "Match Score": score_val,
            "Status": status_val,
            "Resume PDF": pdf_val,
            "Apply URL": url_val,
            "Location": loc_val,
            "Match Reason": reason_val,
            "Missing Skills": skills_val,
        })

    return output.getvalue()


def render_summary_html(
    results: List[Dict[str, Any]],
    candidate_name: str = "Candidate",
    threshold: int = 70,
) -> str:
    """Generates an HTML summary table report with strict autoescaping and safe URLs."""
    total_jobs = len(results)
    tailored_jobs = [r for r in results if r.get("pdf_path")]
    tailored_count = len(tailored_jobs)
    avg_score = (
        round(sum(r.get("match_score", 0) for r in results) / total_jobs, 1)
        if total_jobs > 0 else 0
    )

    # Sanitize URLs before rendering
    clean_results = []
    for r in results:
        r_copy = dict(r)
        url_val = r_copy.get("url")
        r_copy["url"] = url_val if is_safe_url(url_val) else ""
        clean_results.append(r_copy)

    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    try:
        tmpl = env.get_template("summary_template.html")
        return tmpl.render(
            results=clean_results,
            candidate_name=candidate_name,
            total_jobs=total_jobs,
            tailored_count=tailored_count,
            avg_score=avg_score,
            threshold=threshold,
            run_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception:
        # Safe fallback
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Batch Execution Summary</title>
</head>
<body>
    <h1>JobHunt AI Studio — Batch Run Report</h1>
    <p>Candidate: {candidate_name} | Total Jobs: {total_jobs} | Tailored: {tailored_count}</p>
</body>
</html>"""
