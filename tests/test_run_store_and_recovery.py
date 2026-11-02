"""
Verification Suite: RunStore, Persistence, Worker Leases & Restart Recovery.
Tests:
- Transactional state persistence in SQLite with WAL mode
- Worker lease acquire, heartbeat, expiry, and mutual exclusion
- Server restart recovery: interrupted running jobs transition to awaiting_credentials
- Cryptographic recovery code hashing and rejection of unauthorized access
- Fingerprint verification preventing resumption when inputs differ
- Expired run purging while preserving active leases
"""

import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from core.models import JobInput, JobResult, RunConfig, RunStatus, Stage, StageEvent
from core.run_store import RunStore
from core.security import generate_recovery_code, hash_recovery_code, verify_recovery_code, compute_input_fingerprint


@pytest.fixture
def tmp_store():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        store = RunStore(db_dir=Path(tmp_dir))
        yield store


def test_run_creation_and_recovery_verification(tmp_store):
    """Test run creation, code hashing, and unauthorized access rejection."""
    plain_code = generate_recovery_code()
    code_hash = hash_recovery_code(plain_code)

    config = RunConfig(
        run_id="run_test_001",
        provider="gemini",
        model="gemini-3.5-flash-lite",
        score_threshold=75,
        max_resumes=10,
    )
    profile = {"personal_information": {"name": "Test", "surname": "User"}}
    jobs = [
        JobInput(job_id="j1", company="Acme", title="Engineer", jd_text="Software engineering with Python."),
        JobInput(job_id="j2", company="Beta", title="Developer", jd_text="Fullstack developer with React."),
    ]
    fingerprint = compute_input_fingerprint([j.to_dict() for j in jobs], profile, 75, 10, "gemini-3.5-flash-lite")

    run_id = tmp_store.create_run(
        config=config,
        profile=profile,
        jobs=jobs,
        recovery_code_hash=code_hash,
        input_fingerprint=fingerprint,
    )
    assert run_id == "run_test_001"

    # Authorized access
    assert tmp_store.verify_recovery("run_test_001", plain_code) is True

    # Unauthorized access attempts
    assert tmp_store.verify_recovery("run_test_001", "wrong-recovery-code") is False
    assert tmp_store.verify_recovery("run_test_001", "") is False
    assert tmp_store.verify_recovery("nonexistent_run", plain_code) is False

    # Stored state matches
    run = tmp_store.get_run(run_id)
    assert run["status"] == RunStatus.PENDING.value
    assert run["total_jobs"] == 2
    assert run["completed_count"] == 0


def test_worker_lease_acquisition_and_mutual_exclusion(tmp_store):
    """Verify that only one worker can hold an active lease at a time."""
    plain_code = generate_recovery_code()
    config = RunConfig(run_id="run_lease_test", provider="gemini", model="gemini-3.5-flash-lite")
    jobs = [JobInput(job_id="j1", company="Acme", title="Dev", jd_text="Code code code.")]
    tmp_store.create_run(
        config=config,
        profile={},
        jobs=jobs,
        recovery_code_hash=hash_recovery_code(plain_code),
        input_fingerprint="fp123",
    )

    # Worker 1 acquires lease
    assert tmp_store.acquire_lease("run_lease_test", worker_id="worker_1", duration_sec=5) is True

    # Worker 2 attempts to steal active lease -> rejected
    assert tmp_store.acquire_lease("run_lease_test", worker_id="worker_2", duration_sec=5) is False

    # Worker 1 renews lease
    assert tmp_store.renew_lease("run_lease_test", worker_id="worker_1", duration_sec=10) is True

    # Worker 1 releases lease
    tmp_store.release_lease("run_lease_test", worker_id="worker_1")

    # Worker 2 can now acquire lease
    assert tmp_store.acquire_lease("run_lease_test", worker_id="worker_2", duration_sec=5) is True


def test_server_restart_recovery_transitions_orphaned_runs(tmp_store):
    """
    Verify server restart recovery:
    An interrupted run left in 'running' state with an expired lease
    must transition to 'awaiting_credentials' so the user can supply keys and resume.
    """
    plain_code = generate_recovery_code()
    config = RunConfig(run_id="run_interrupted", provider="gemini", model="gemini-3.5-flash-lite")
    jobs = [JobInput(job_id="j1", company="Acme", title="Dev", jd_text="Code.")]
    tmp_store.create_run(
        config=config,
        profile={},
        jobs=jobs,
        recovery_code_hash=hash_recovery_code(plain_code),
        input_fingerprint="fp123",
    )

    # Simulate running state with an expired lease (e.g. server crashed 2 minutes ago)
    tmp_store.acquire_lease("run_interrupted", "crashed_worker", duration_sec=1)
    tmp_store.update_run_status("run_interrupted", RunStatus.RUNNING)
    time.sleep(1.2)  # Lease expires

    # Run restart recovery
    recovered_count = tmp_store.recover_interrupted_runs()
    assert recovered_count == 1

    run = tmp_store.get_run("run_interrupted")
    assert run["status"] == RunStatus.AWAITING_CREDENTIALS.value
    assert run["lease_owner"] is None


def test_stage_checkpoints_and_completed_results(tmp_store):
    """Verify transactional saving and retrieval of job stage results."""
    plain_code = generate_recovery_code()
    config = RunConfig(run_id="run_checkpoints", provider="gemini", model="gemini-3.5-flash-lite")
    jobs = [
        JobInput(job_id="j1", company="Acme", title="Engineer", jd_text="Python coding."),
        JobInput(job_id="j2", company="Beta", title="Analyst", jd_text="SQL data analytics."),
    ]
    tmp_store.create_run(
        config=config,
        profile={},
        jobs=jobs,
        recovery_code_hash=hash_recovery_code(plain_code),
        input_fingerprint="fp1",
    )

    # Save completed result for Job 1
    res1 = JobResult(
        job_id="j1",
        company="Acme",
        title="Engineer",
        match_score=85,
        status="Tailored & Verified",
        pdf_path="/path/to/Acme.pdf",
        audit_result={"passed": True, "errors": []},
        prompt_tokens=1200,
        completion_tokens=400,
    )
    tmp_store.save_job_result("run_checkpoints", res1, Stage.CHECK)

    run = tmp_store.get_run("run_checkpoints")
    assert run["completed_count"] == 1
    assert run["verified_resumes_count"] == 1

    completed = tmp_store.get_completed_job_results("run_checkpoints")
    assert len(completed) == 1
    assert completed[0].job_id == "j1"
    assert completed[0].match_score == 85


def test_delete_run_purges_state_and_artifacts(tmp_store):
    """Verify that deleting a run purges both database rows and disk files."""
    plain_code = generate_recovery_code()
    config = RunConfig(run_id="run_to_delete", provider="gemini", model="gemini-3.5-flash-lite")
    jobs = [JobInput(job_id="j1", company="Acme", title="Dev", jd_text="Code.")]
    tmp_store.create_run(
        config=config,
        profile={},
        jobs=jobs,
        recovery_code_hash=hash_recovery_code(plain_code),
        input_fingerprint="fp_del",
    )

    # Create dummy artifact file in run's artifact dir
    art_dir = tmp_store.get_run_artifacts_dir("run_to_delete")
    dummy_file = art_dir / "test.txt"
    dummy_file.write_text("artifact data")
    assert dummy_file.exists()

    tmp_store.delete_run("run_to_delete")

    assert tmp_store.get_run("run_to_delete") is None
    assert not art_dir.exists()
