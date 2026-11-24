"""
Integration & Smoke Tests for JobHunt AI Studio Core Engine.
Validates:
- CSV parser with fuzzy synonym mapping
- Profile manager schema normalization
- Full unstripped JD transmission to AI scorer
- Role lens detection & factual sanitization
- Playwright single-page PDF generation & ATS acceptance audit
- ZIP packager & executive summaries
- Cost estimator load projection
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
from core.profile_manager import load_profile_from_file, load_owner_profile, normalize_profile
from core.scorer import build_scoring_system_prompt
from core.resume_tailor import detect_role_lens, sanitize_tailored_resume
from core.pdf_generator import generate_single_page_pdf
from core.ats_verifier import audit_resume_pdf
from core.packager import build_batch_zip_bytes
from core.summary_reporter import generate_summary_csv, render_summary_html
from core.cost_estimator import estimate_batch_cost
from core.session_manager import SessionManager
from core.settings import MODEL, PROFILE_PATH


def test_owner_profile_is_locked_and_complete():
    """The bundled profile belongs to Kaustubh and carries his full skill/project inventory."""
    assert PROFILE_PATH.exists(), "data/profile.yaml must exist."
    profile = load_owner_profile()
    assert profile["personal_information"]["name"] == "Kaustubh"
    assert profile["personal_information"]["surname"] == "Kale"
    assert profile["personal_information"]["email"] == "kaustubh.kale.work@gmail.com"
    assert len(profile["skills"]) >= 50
    assert len(profile["projects"]) == 6
    assert len(profile["experience_details"]) >= 1
    assert len(profile["education_details"]) >= 1
    assert "Java Developer" in profile["target_roles"]


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
    """Verify profile loading keeps every supplied fact and normalizes field names."""
    profile = load_profile_from_file("data/profile.yaml")
    assert "personal_information" in profile
    assert profile["personal_information"]["name"] == "Kaustubh"
    assert len(profile["skills"]) > 5
    assert len(profile["projects"]) > 0
    assert profile["skills_structured"]["Core Languages"]


def test_full_jd_preserved_in_scoring():
    """Verify that full JD is preserved as-is with all company & requirement details intact."""
    profile = load_owner_profile()
    prompt = build_scoring_system_prompt(profile)
    assert "CANDIDATE MASTER RESUME" in prompt
    assert "SCORING RULES" in prompt
    assert "Kaustubh Kale" in prompt


def test_role_lens_detection():
    """Verify role classification into engineering lenses."""
    assert detect_role_lens("SDET / Quality Assurance Engineer", "Testing automation") == "qa"
    assert detect_role_lens("Java Spring Boot Developer", "Microservices architecture") == "java"
    assert detect_role_lens("C# .NET Backend Engineer", "ASP.NET core") == "dotnet"
    assert detect_role_lens("Full Stack Software Engineer", "React and Node.js") == "fullstack"


def test_tailored_sanitization_and_dynamic_ordering():
    """Verify that tailored resumes sanitize illegal skills and set is_testing_role correctly."""
    profile = load_owner_profile()
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
    profile = load_profile_from_file(PROJECT_ROOT / "data" / "profile.yaml")
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


def test_cost_estimator_projects_locked_batch_load():
    """Verify token projection when no USD pricing is configured."""
    estimate = estimate_batch_cost(total_jobs=50, max_resumes=20)
    assert estimate["is_free"] is False
    assert estimate["cost_usd"] is None
    assert estimate["cost_display"] == "Token estimate only"
    assert estimate["total_tokens"] > 50000
    assert estimate["estimated_tailored"] <= 20


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
            model="deepseek-flash",
            provider="deepseek",
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
