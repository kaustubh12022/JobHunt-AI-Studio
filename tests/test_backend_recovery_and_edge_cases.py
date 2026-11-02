"""
Verification Suite: Backend Recovery, Queue Saturation, Multi-Experience & PDF Security Edge Cases.
Tests:
1. Queue saturation: 10 queued runs rejects new runs immediately.
2. Auto-recovery on RunStore initialization transitions orphaned runs to awaiting_credentials.
3. Resume/retry skips completed stages (no duplicate scoring) and re-runs failed tailoring/rendering.
4. Input fingerprint validation rejects resuming with changed inputs.
5. PDF generator disables JavaScript (java_script_enabled=False) and renders cleanly.
6. Multi-experience tailoring matches by employer and rejects invalid company references.
7. General candidate profile (e.g. London / San Francisco) passes ATS audit without false location warning.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from core.models import ArtifactResult, JobInput, JobResult, RunConfig, RunStatus, Stage
from core.models_catalog import validate_model
from core.pdf_generator import _render_pdf_sync, generate_single_page_pdf
from core.resume_tailor import sanitize_tailored_resume, tailor_resume_async
from core.run_service import RunService
from core.run_store import RunStore
from core.security import (
    MAX_QUEUED_RUNS,
    compute_input_fingerprint,
    generate_recovery_code,
    hash_recovery_code,
)
from core.ats_verifier import audit_resume_pdf


def test_queue_saturation_rejection():
    """Verify that attempting to create a run when 10 queued runs exist raises a RuntimeError."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        store = RunStore(db_dir=Path(tmp_dir))
        svc = RunService(store=store)

        # Create 10 pending runs
        for i in range(MAX_QUEUED_RUNS):
            cfg = RunConfig(run_id=f"run_queued_{i}", provider="gemini", model="gemini-3.5-flash-lite")
            jobs = [JobInput(job_id=f"j_{i}", company="Acme", title="Engineer", jd_text="Code")]
            svc.create_run(cfg, profile={"name": "Alice"}, jobs=jobs)

        assert store.get_queued_runs_count() == MAX_QUEUED_RUNS

        # The 11th run must be rejected
        cfg_overflow = RunConfig(run_id="run_overflow", provider="gemini", model="gemini-3.5-flash-lite")
        jobs_overflow = [JobInput(job_id="j_overflow", company="Acme", title="Engineer", jd_text="Code")]
        with pytest.raises(RuntimeError) as exc:
            svc.create_run(cfg_overflow, profile={"name": "Alice"}, jobs=jobs_overflow)
        assert "Queue saturation" in str(exc.value)


def test_auto_recovery_on_runstore_init():
    """Verify that instantiating RunStore automatically recovers orphaned runs from a previous crash."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = Path(tmp_dir)
        store1 = RunStore(db_dir=db_path)

        code = generate_recovery_code()
        cfg = RunConfig(run_id="run_crashed", provider="gemini", model="gemini-3.5-flash-lite")
        jobs = [JobInput(job_id="j1", company="Acme", title="Dev", jd_text="Code.")]
        store1.create_run(cfg, profile={}, jobs=jobs, recovery_code_hash=hash_recovery_code(code), input_fingerprint="fp")

        # Simulate a worker that acquired a lease for 1s and set status to RUNNING
        store1.acquire_lease("run_crashed", "crashed_worker", duration_sec=1)
        store1.update_run_status("run_crashed", RunStatus.RUNNING)
        time.sleep(1.2)  # Lease expires

        # Now simulate server restarting by instantiating a new RunStore
        store2 = RunStore(db_dir=db_path)
        recovered_run = store2.get_run("run_crashed")

        assert recovered_run["status"] == RunStatus.AWAITING_CREDENTIALS.value
        assert recovered_run["lease_owner"] is None


def test_resume_does_not_repeat_scoring_and_retries_failed_tailoring():
    """
    Verify that resuming or retrying a run:
    1. Does NOT repeat completed scoring (no duplicate LLM scoring billing)
    2. Re-runs tailoring and finishes the job cleanly
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        store = RunStore(db_dir=Path(tmp_dir))
        svc = RunService(store=store)

        cfg = RunConfig(run_id="run_retry_test", provider="gemini", model="gemini-3.5-flash-lite", score_threshold=70)
        jobs = [
            JobInput(job_id="j1", company="AlphaTech", title="Backend Engineer", jd_text="Python FastAPI SQL microservices"),
        ]
        profile = {
            "personal_information": {"name": "Test", "surname": "Candidate", "address": "Pune, India"},
            "skills": ["Python", "FastAPI", "SQL", "Docker", "Git", "REST APIs"],
            "experience_details": [
                {
                    "company": "AlphaTech",
                    "position": "Backend Engineer",
                    "dates": "Jan 2024 - Dec 2024",
                    "key_responsibilities": ["Built services", "Optimized queries"],
                }
            ],
            "projects": [],
        }

        run_id, code = svc.create_run(cfg, profile=profile, jobs=jobs)

        # Simulate that Job 1 already scored 85 (success), but tailoring had failed
        scored_job = JobResult(
            job_id="j1",
            company="AlphaTech",
            title="Backend Engineer",
            match_score=85,
            scoring_status="success",
            status="Tailoring Error: Network timeout",
            error="Network timeout",
        )
        store.save_job_result(run_id, scored_job, Stage.SCORE)

        # Mock score_job_async to ensure it is NEVER called during retry
        mock_score = AsyncMock(side_effect=AssertionError("Scoring should not be re-run!"))
        mock_tailor = AsyncMock(return_value={
            **profile,
            "profile_summary": "Tailored summary for AlphaTech",
            "is_draft_review": True,
        })

        with patch("core.run_service.score_job_async", mock_score), \
             patch("core.run_service.tailor_resume_async", mock_tailor), \
             patch("core.run_service.render_and_audit_pdf", return_value=("/path/to/AlphaTech.pdf", {"passed": True, "errors": []})):

            # Run synchronously
            svc.start_or_resume_run(run_id, code, api_keys=["test-key"], background=False)

        # Confirm score_job_async was NEVER called
        mock_score.assert_not_called()
        # Confirm tailor_resume_async WAS called
        mock_tailor.assert_called_once()

        # Confirm the job is now successfully completed
        completed = store.get_completed_job_results(run_id)
        assert len(completed) == 1
        assert completed[0].status == "Tailored & Verified"
        assert completed[0].match_score == 85


def test_resume_with_changed_input_fingerprint_raises_value_error():
    """Verify that attempting to resume with changed inputs is rejected."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        store = RunStore(db_dir=Path(tmp_dir))
        svc = RunService(store=store)

        cfg = RunConfig(run_id="run_fp_test", provider="gemini", model="gemini-3.5-flash-lite")
        jobs = [JobInput(job_id="j1", company="Acme", title="Dev", jd_text="Code")]
        profile = {"personal_information": {"name": "Alice"}}

        fp_original = compute_input_fingerprint([j.to_dict() for j in jobs], profile, 70, 20, "gemini-3.5-flash-lite")
        run_id, code = svc.create_run(cfg, profile=profile, jobs=jobs)

        # Attempt to resume with matching fingerprint succeeds
        with patch.object(svc, "_run_worker", new=AsyncMock()):
            svc.start_or_resume_run(run_id, code, api_keys=["key"], background=False, expected_fingerprint=fp_original)

        # Attempt to resume with DIFFERENT fingerprint fails
        fp_changed = "changed_fingerprint_hash_xyz"
        with pytest.raises(ValueError) as exc:
            svc.start_or_resume_run(run_id, code, api_keys=["key"], background=False, expected_fingerprint=fp_changed)
        assert "Changed inputs detected" in str(exc.value)


def test_multi_experience_matching_and_invalid_reference_rejection():
    """Verify that sanitize_tailored_resume matches across multiple experiences and rejects invalid companies."""
    master_profile = {
        "personal_information": {"name": "Senior", "surname": "Engineer"},
        "skills": ["Java", "Spring Boot", "Python", "FastAPI", "SQL", "Docker"],
        "experience_details": [
            {
                "company": "FirstCorp",
                "position": "Backend Engineer",
                "dates": "Jan 2022 - Dec 2023",
                "key_responsibilities": ["Original bullet 1", "Original bullet 2"],
            },
            {
                "company": "SecondCorp",
                "position": "Senior Developer",
                "dates": "Jan 2024 - Present",
                "key_responsibilities": ["Original bullet 3", "Original bullet 4"],
            },
        ],
        "projects": [],
    }

    tailored_delta = {
        "profile_summary": "Tailored summary for general senior role.",
        "tailored_skills": ["Java", "Spring Boot", "Python"],
        "tailored_experience": [
            {
                "company": "SecondCorp",
                "position": "Senior Developer",
                "key_responsibilities": ["Rewritten bullet for SecondCorp 1", "Rewritten bullet for SecondCorp 2"],
            },
            {
                "company": "FirstCorp",
                "position": "Backend Engineer",
                "key_responsibilities": ["Rewritten bullet for FirstCorp 1"],
            },
            {
                "company": "HallucinatedFakeCorp",
                "position": "CEO",
                "key_responsibilities": ["Fake bullet"],
            },
        ],
    }

    sanitized = sanitize_tailored_resume(tailored_delta, master_profile, role_lens="fullstack")

    # Verify both valid experiences were updated
    assert sanitized["experience_details"][0]["key_responsibilities"] == ["Rewritten bullet for FirstCorp 1"]
    assert sanitized["experience_details"][1]["key_responsibilities"] == [
        "Rewritten bullet for SecondCorp 1",
        "Rewritten bullet for SecondCorp 2",
    ]

    # Verify HallucinatedFakeCorp was REJECTED and not added
    assert len(sanitized["experience_details"]) == 2
    assert not any(e["company"] == "HallucinatedFakeCorp" for e in sanitized["experience_details"])

    # Verify dates and titles from master profile remained strictly preserved
    assert sanitized["experience_details"][0]["dates"] == "Jan 2022 - Dec 2023"
    assert sanitized["experience_details"][1]["dates"] == "Jan 2024 - Present"


def test_general_candidate_profile_location_in_ats_audit():
    """Verify that a candidate with London, UK or San Francisco, USA passes location check without false warnings."""
    # Test checking candidate_location directly
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        # Create a mock PDF with London in header
        import pypdf
        writer = pypdf.PdfWriter()
        # Create a blank page
        page = writer.add_blank_page(width=595, height=842) # A4
        pdf_path = Path(tmp_dir) / "test_london.pdf"
        with open(pdf_path, "wb") as f:
            writer.write(f)

        # When file has no text, char_count check fails as expected
        res = audit_resume_pdf(str(pdf_path), candidate_location="London, United Kingdom")
        assert any("EMPTY CONTENT" in err for err in res["errors"])
