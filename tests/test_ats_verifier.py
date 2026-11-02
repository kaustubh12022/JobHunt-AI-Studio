"""
Universal ATS Acceptance Test Suite for JobHunt AI Studio.
Tests that ats_verifier correctly catches flaws and accepts compliant resumes.
"""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from core.ats_verifier import audit_resume_pdf
from core.pdf_generator import generate_single_page_pdf
from core.profile_manager import load_profile_from_file


def test_ats_acceptance_on_sample_profile():
    """Verify that sample profile renders to a 100% ATS compliant PDF."""
    sample_yaml = PROJECT_ROOT / "data" / "sample_profile.yaml"
    profile = load_profile_from_file(sample_yaml)

    job = {
        "company": "Tata_Consultancy_Services",
        "title": "Software_Engineer",
        "location": "Pune, India",
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = generate_single_page_pdf(profile, output_dir=tmp_dir, job=job)
        assert os.path.exists(pdf_path)

        result = audit_resume_pdf(pdf_path)
        assert result["passed"] is True, f"ATS check failed: {result['errors']}"
        assert result["details"]["page_count"] == 1
        assert len(result["errors"]) == 0


def test_ats_audit_detects_nonexistent_file():
    """Verify handling of missing files."""
    result = audit_resume_pdf("non_existent_file_12345.pdf")
    assert result["passed"] is False
    assert any("FILE NOT FOUND" in err for err in result["errors"])
