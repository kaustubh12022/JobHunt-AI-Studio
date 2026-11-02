"""
Verification Suite: Safety, Validation & Neutralization.
Tests:
- CSV formula injection neutralization (prevention of DDE exploits in Excel/Calc)
- Safe URL protocol enforcement (blocking javascript:, data:, file: URIs)
- Oversized JD rejection (> 30,000 characters) without silent truncation
- Oversized file upload limits (5 MB CSV, 1 MB Profile)
- Stable job ID generation and collision-free filename formatting
- Elimination of invented defaults (missing location stays empty / Not specified)
- HTML template autoescaping preventing XSS injection
"""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from core.csv_parser import parse_jobs_csv
from core.profile_manager import load_profile_from_str, normalize_profile
from core.security import (
    build_safe_pdf_filename,
    compute_stable_job_id,
    is_safe_url,
    neutralize_csv_cell,
    sanitize_url,
)
from core.summary_reporter import generate_summary_csv, render_summary_html


def test_csv_formula_injection_neutralization():
    """Verify that dangerous leading characters are prefixed with a single quote."""
    dangerous_inputs = [
        "=CMD('calc.exe')",
        "+1+2+cmd|' /C calc'!A0",
        "-2+3+cmd|' /C calc'!A0",
        "@SUM(A1:A10)",
        "\tmalicious_tab",
        "\rmalicious_cr",
    ]
    for inp in dangerous_inputs:
        neutralized = neutralize_csv_cell(inp)
        assert neutralized.startswith("'"), f"Failed to neutralize: {inp}"
        assert neutralized[1:] == inp

    # Safe inputs are preserved as-is
    assert neutralize_csv_cell("Software Engineer") == "Software Engineer"
    assert neutralize_csv_cell("Google, Inc.") == "Google, Inc."
    assert neutralize_csv_cell("") == ""
    assert neutralize_csv_cell(None) == ""


def test_summary_csv_neutralization_end_to_end():
    """Verify that generate_summary_csv neutralizes formulas across all fields."""
    results = [
        {
            "company": "=HYPERLINK('http://malicious.site')",
            "title": "+Admin Role",
            "match_score": 85,
            "status": "-Evaluated",
            "pdf_path": "@test.pdf",
            "url": "=cmd|' /C calc'!A0",
            "location": "\tPune",
            "reason": "=1+1",
            "missing_skills": ["@AWS"],
        }
    ]
    csv_str = generate_summary_csv(results)
    assert "'=HYPERLINK" in csv_str
    assert "'+Admin Role" in csv_str
    assert "'-Evaluated" in csv_str
    assert "'=1+1" in csv_str
    assert "'@AWS" in csv_str


def test_safe_url_validation():
    """Verify safe URL protocol enforcement."""
    # Safe URLs
    assert is_safe_url("https://careers.google.com/jobs/123") is True
    assert is_safe_url("http://innovaccer.com/careers") is True

    # Malicious or dangerous schemes
    assert is_safe_url("javascript:alert(document.cookie)") is False
    assert is_safe_url("data:text/html,<script>alert(1)</script>") is False
    assert is_safe_url("file:///etc/passwd") is False
    assert is_safe_url("vbscript:msgbox(1)") is False
    assert is_safe_url("") is False
    assert is_safe_url("N/A") is False
    assert is_safe_url(None) is False

    # sanitize_url returns clean url or empty string
    assert sanitize_url("https://example.com") == "https://example.com"
    assert sanitize_url("javascript:alert(1)") == ""


def test_oversized_jd_rejected_without_silent_truncation():
    """Verify that job descriptions exceeding 30,000 characters are rejected with an actionable error."""
    oversized_jd = "A" * 30_500
    csv_content = f"""Company,Job Title,Job Description\nAcme,Developer,{oversized_jd}\n"""
    with pytest.raises(ValueError) as exc:
        parse_jobs_csv(csv_content)
    assert "exceeds 30000 characters" in str(exc.value)


def test_oversized_csv_and_profile_limits():
    """Verify 5 MB CSV limit and 1 MB profile limit."""
    # CSV > 5 MB
    huge_csv = b"Company,Job Title,Job Description\n" + (b"A" * (6 * 1024 * 1024))
    with pytest.raises(ValueError) as exc:
        parse_jobs_csv(huge_csv)
    assert "5 MB limit" in str(exc.value)

    # Profile > 1 MB
    huge_profile = "name: " + ("X" * (1024 * 1024 + 100))
    with pytest.raises(ValueError) as exc:
        load_profile_from_str(huge_profile)
    assert "1 MB limit" in str(exc.value)


def test_collision_free_pdf_filename_with_stable_job_id():
    """Verify that distinct jobs with the same company and title receive unique filenames."""
    id1 = compute_stable_job_id("Innovaccer", "Software Engineer", "Java microservices JD", row_index=2)
    id2 = compute_stable_job_id("Innovaccer", "Software Engineer", "React frontend JD", row_index=3)

    assert id1 != id2

    fn1 = build_safe_pdf_filename("Innovaccer", "Software Engineer", id1, "Candidate_One")
    fn2 = build_safe_pdf_filename("Innovaccer", "Software Engineer", id2, "Candidate_One")

    assert fn1 != fn2
    assert id1 in fn1
    assert id2 in fn2


def test_no_invented_location_defaults():
    """Verify that missing location in CSV does not fabricate 'Pune, India / Remote'."""
    csv_content = """Company,Job Title,Job Description\nAcme,Software Engineer,Writing clean and modular backend microservices with Python, FastAPI, and SQL.\n"""
    jobs, _, _ = parse_jobs_csv(csv_content)
    assert len(jobs) == 1
    # Location should be 'Not specified' or empty, never an invented city
    assert jobs[0]["location"] == "Not specified"
    assert "Pune" not in jobs[0]["location"]


def test_html_autoescape_in_summary_and_resume():
    """Verify that raw script tags in candidate or company fields are autoescaped."""
    results = [
        {
            "company": "<script>alert('pwned')</script>",
            "title": "<img src=x onerror=alert(1)>",
            "match_score": 90,
            "status": "Evaluated",
            "url": "https://example.com",
            "reason": "<b>Reason</b>",
        }
    ]
    html = render_summary_html(results, candidate_name="<script>hack</script>", threshold=70)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html
    assert "&lt;img" in html
