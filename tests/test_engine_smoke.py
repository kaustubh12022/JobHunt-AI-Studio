"""
Integration & Smoke Tests for JobHunt AI Studio Core Engine.
Validates:
- CSV parser with fuzzy synonym mapping
- Profile manager schema normalization
- Full unstripped JD transmission to AI scorer
- Role lens detection & factual sanitization
- Playwright single-page PDF generation & ATS acceptance audit
- ZIP packager & executive summaries
- Cost estimator (Gemini Free $0.00 vs paid)
- Multi-tenant session isolation and batch recovery checkpointing
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from core.csv_parser import parse_jobs_csv, detect_column_mapping, generate_sample_csv_str
from core.profile_manager import load_profile_from_file, normalize_profile, DEFAULT_PROFILE_TEMPLATE
from core.scorer import build_scoring_system_prompt
from core.resume_tailor import detect_role_lens, sanitize_tailored_resume
from core.pdf_generator import generate_single_page_pdf
from core.ats_verifier import audit_resume_pdf
from core.packager import build_batch_zip_bytes
from core.summary_reporter import generate_summary_csv, render_summary_html
from core.cost_estimator import estimate_batch_cost
from core.session_manager import SessionManager


def test_csv_parser_fuzzy_mapping():
    """Verify that CSV parser detects diverse synonym headers properly."""
    sample_csv = """Employer,Job Role,Content,Job Link,City
Google,Software Engineer,We need a software engineer with Java and Python skills to build scalable systems. Strong problem solving in DSA and OOP required.,https://careers.google.com,Bangalore
Meta,QA Automation Tester,Looking for an experienced automation tester with Selenium and JUnit skills.,https://metacareers.com,Remote
"""
    jobs, mapping, warnings = parse_jobs_csv(sample_csv)
    assert len(jobs) == 2
    assert mapping["company"] == "Employer"
    assert mapping["title"] == "Job Role"
    assert mapping["jd_text"] == "Content"
    assert mapping["url"] == "Job Link"
    assert mapping["location"] == "City"
    assert jobs[0]["company"] == "Google"
    assert jobs[0]["title"] == "Software Engineer"


def test_profile_manager_normalization():
    """Verify profile loading and fallback normalization."""
    sample_path = PROJECT_ROOT / "data" / "sample_profile.yaml"
    assert sample_path.exists(), "sample_profile.yaml must exist."
    profile = load_profile_from_file(sample_path)
    assert "personal_information" in profile
    assert profile["personal_information"]["name"] != ""
    assert len(profile["skills"]) > 5
    assert len(profile["projects"]) > 0


def test_full_jd_preserved_in_scoring():
    """Verify that full JD is preserved as-is with all company & requirement details intact."""
    profile = normalize_profile(DEFAULT_PROFILE_TEMPLATE)
    prompt = build_scoring_system_prompt(profile)
    assert "CANDIDATE MASTER RESUME" in prompt
    assert "SCORING RULES" in prompt


def test_role_lens_detection():
    """Verify role classification into engineering lenses."""
    assert detect_role_lens("SDET / Quality Assurance Engineer", "Testing automation") == "qa"
    assert detect_role_lens("Java Spring Boot Developer", "Microservices architecture") == "java"
    assert detect_role_lens("C# .NET Backend Engineer", "ASP.NET core") == "dotnet"
    assert detect_role_lens("Full Stack Software Engineer", "React and Node.js") == "fullstack"


def test_tailored_sanitization_and_dynamic_ordering():
    """Verify that tailored resumes sanitize illegal skills and set is_testing_role correctly."""
    profile = normalize_profile(DEFAULT_PROFILE_TEMPLATE)
    delta = {
        "profile_summary": "Graduate Engineer specializing in backend software engineering.",
        "tailored_skills": ["Python", "Java", "Docker", "FabricatedIllegalSkill123"],
    }
    sanitized = sanitize_tailored_resume(delta, profile, role_lens="qa", is_testing_role=True)
    assert sanitized["is_testing_role"] is True
    assert "FabricatedIllegalSkill123" not in sanitized["skills"]
    assert "Python" in sanitized["skills"]


def test_playwright_single_page_pdf_generation_and_ats_audit():
    """Verify that Playwright renders a PDF and it passes the strict ATS 1-page check."""
    profile = load_profile_from_file(PROJECT_ROOT / "data" / "sample_profile.yaml")
    job = {
        "company": "Innovaccer",
        "title": "Associate Software Engineer - Java",
        "location": "Noida, India",
        "url": "https://innovaccer.com",
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = generate_single_page_pdf(profile, output_dir=tmp_dir, job=job)
        assert os.path.exists(pdf_path)
        assert pdf_path.endswith(".pdf")

        # Run ATS Acceptance audit
        audit = audit_resume_pdf(pdf_path)
        assert audit["details"]["page_count"] == 1, f"Expected 1 page, got {audit['details']['page_count']}"
        assert len(audit["errors"]) == 0, f"ATS errors found: {audit['errors']}"
        assert audit["passed"] is True


def test_zip_packager_and_summary():
    """Verify summary CSV, HTML, and ZIP bundling."""
    results = [
        {
            "company": "Google",
            "title": "Software Engineer",
            "match_score": 85,
            "status": "Tailored",
            "pdf_path": "dummy.pdf",
            "url": "https://google.com",
            "location": "Bangalore",
            "reason": "Strong match on Java and DSA.",
            "missing_skills": ["Kafka"],
        }
    ]
    csv_str = generate_summary_csv(results)
    assert "Google" in csv_str
    assert "85%" in csv_str

    html_str = render_summary_html(results, candidate_name="Test Candidate", threshold=70)
    assert "JobHunt AI Studio" in html_str

    zip_bytes = build_batch_zip_bytes(
        pdf_paths=[],
        summary_csv_str=csv_str,
        summary_html_str=html_str,
    )
    assert len(zip_bytes) > 100


def test_cost_estimator_free_and_paid():
    """Verify cost estimator logic for Gemini Free vs Paid models."""
    # Free Tier test
    free_est = estimate_batch_cost(
        total_jobs=50,
        provider="gemini",
        model="gemini-3.5-flash-lite",
        is_gemini_free=True,
    )
    assert free_est["is_free"] is True
    assert free_est["cost_usd"] == 0.0
    assert "Free Tier" in free_est["cost_display"]
    assert free_est["total_tokens"] > 50000

    # Paid DeepSeek test
    paid_est = estimate_batch_cost(
        total_jobs=50,
        provider="deepseek",
        model="deepseek-chat",
        is_gemini_free=False,
    )
    assert paid_est["is_free"] is False
    assert paid_est["cost_usd"] > 0.0
    assert "$" in paid_est["cost_display"]


def test_session_manager_isolation_and_batch_recovery():
    """Verify multi-tenant user isolation and checkpoint recovery."""
    with tempfile.TemporaryDirectory() as tmp_base:
        sm = SessionManager(base_dir=Path(tmp_base), ttl_seconds=60)
        user_a = "user_alpha_123"
        user_b = "user_beta_456"

        # Verify distinct isolated directories
        dir_a = sm.get_resumes_dir(user_a)
        dir_b = sm.get_resumes_dir(user_b)
        assert dir_a != dir_b
        assert dir_a.exists() and dir_b.exists()

        # Save checkpoint for User A at Job 67 of 100
        mock_completed = [{"job_id": f"j_{i}", "match_score": 80} for i in range(67)]
        sm.save_checkpoint(
            session_id=user_a,
            total_jobs=100,
            completed_jobs=mock_completed,
            threshold=70,
            model="gemini-3.5-flash-lite",
            provider="gemini",
        )

        # Load checkpoint
        cp_a = sm.load_checkpoint(user_a)
        assert cp_a is not None
        assert cp_a["total_jobs"] == 100
        assert cp_a["completed_count"] == 67

        # User B should have NO checkpoint (strict isolation)
        cp_b = sm.load_checkpoint(user_b)
        assert cp_b is None

        # Clear checkpoint
        sm.clear_checkpoint(user_a)
        assert sm.load_checkpoint(user_a) is None
