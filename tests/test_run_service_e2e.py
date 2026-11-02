"""
Verification Suite: End-to-End RunService, Isolation, Packaging & Health.
Tests:
- Multi-run isolation with distinct recovery codes
- Independent packaging retry without re-running earlier stages
- Unverified/failed PDFs excluded from verified bundle
- System health diagnostic verification
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from core.health import perform_system_health_check
from core.models import ArtifactResult, JobInput, JobResult, RunConfig, RunStatus, Stage
from core.run_service import RunService
from core.run_store import RunStore


@pytest.fixture
def service_env():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        store = RunStore(db_dir=Path(tmp_dir))
        svc = RunService(store=store)
        yield svc, store


def test_system_health_check(service_env):
    """Verify that system diagnostics report healthy database and storage."""
    svc, store = service_env
    with patch("core.health.run_store_instance", store):
        health = perform_system_health_check()
        assert health["status"] in ("healthy", "degraded")
        assert health["checks"]["database"]["status"] == "ok"
        assert health["checks"]["storage"]["status"] == "ok"
        assert health["checks"]["storage"]["free_space_mb"] > 0


def test_multi_run_isolation_and_credentials(service_env):
    """Verify that two distinct runs remain strictly isolated and require their own recovery codes."""
    svc, store = service_env

    # Run A
    cfg_a = RunConfig(run_id="run_alpha", provider="gemini", model="gemini-3.5-flash-lite")
    jobs_a = [JobInput(job_id="ja1", company="AlphaCorp", title="Dev", jd_text="Code")]
    run_id_a, code_a = svc.create_run(cfg_a, profile={"name": "Alice"}, jobs=jobs_a)

    # Run B
    cfg_b = RunConfig(run_id="run_beta", provider="openai", model="gpt-4o-mini")
    jobs_b = [JobInput(job_id="jb1", company="BetaCorp", title="Tester", jd_text="Test")]
    run_id_b, code_b = svc.create_run(cfg_b, profile={"name": "Bob"}, jobs=jobs_b)

    assert run_id_a != run_id_b
    assert code_a != code_b

    # Verifying with wrong code fails
    with pytest.raises(PermissionError):
        svc.pause_run(run_id_a, code_b)

    with pytest.raises(PermissionError):
        svc.delete_run(run_id_b, code_a)

    # Authorized actions succeed
    status_a = svc.get_status(run_id_a, code_a)
    assert status_a["is_authorized"] is True
    assert status_a["total_jobs"] == 1


def test_independent_packaging_and_audit_filtering(service_env):
    """
    Verify that packaging:
    1. Can run independently without repeating prior stages
    2. Strictly excludes PDFs that failed the ATS audit from the verified ZIP
    """
    svc, store = service_env

    cfg = RunConfig(run_id="run_pack_test", provider="gemini", model="gemini-3.5-flash-lite")
    jobs = [
        JobInput(job_id="j1", company="GoodCompany", title="Engineer", jd_text="Good JD"),
        JobInput(job_id="j2", company="BadCompany", title="Developer", jd_text="Bad JD"),
    ]
    run_id, code = svc.create_run(cfg, profile={"personal_information": {"name": "Test"}}, jobs=jobs)

    # Create dummy PDF files in artifact dir
    art_dir = store.get_run_artifacts_dir(run_id)
    pdf1 = art_dir / "GoodCompany_Engineer_j1_Test.pdf"
    pdf1.write_bytes(b"%PDF-good-content")

    pdf2 = art_dir / "BadCompany_Developer_j2_Test.pdf"
    pdf2.write_bytes(b"%PDF-bad-content")

    # Record Job 1 as verified, Job 2 as audit failed
    res1 = JobResult(
        job_id="j1",
        company="GoodCompany",
        title="Engineer",
        match_score=90,
        status="Tailored & Verified",
        pdf_path=str(pdf1),
        pdf_filename=pdf1.name,
        audit_result={"passed": True, "errors": []},
    )
    res2 = JobResult(
        job_id="j2",
        company="BadCompany",
        title="Developer",
        match_score=80,
        status="Audit Failed",
        pdf_path=str(pdf2),
        pdf_filename=pdf2.name,
        audit_result={"passed": False, "errors": ["PAGE COUNT VIOLATION"]},
    )
    store.save_job_result(run_id, res1, Stage.CHECK)
    store.save_job_result(run_id, res2, Stage.CHECK)

    # Trigger independent packaging
    artifact_res = svc.package_run_artifacts(run_id, code)

    assert artifact_res.verified_count == 1
    assert len(artifact_res.pdf_paths) == 1
    assert str(pdf1) in artifact_res.pdf_paths
    # Bad PDF MUST NOT be in the verified bundle
    assert str(pdf2) not in artifact_res.pdf_paths

    assert artifact_res.zip_bytes.startswith(b"PK")
    assert "Company,Job Title" in artifact_res.summary_csv_str
    import zipfile, io
    with zipfile.ZipFile(io.BytesIO(artifact_res.zip_bytes)) as z:
        assert "batch_applied_summary.csv" in z.namelist()
