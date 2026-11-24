"""Owner-console UI checks: gate, upload flow, results, recovery and lock.

The studio is intentionally linear (upload -> run -> download), so these tests
assert the screen contract an automated browser agent relies on: one title per
screen, stable labels, and a machine-readable state snapshot.
"""
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from streamlit.testing.v1 import AppTest

from core.profile_manager import load_owner_profile
from core.session_manager import SessionManager
from ui import counts, display_status, filter_results, status_banner

ROOT = Path(__file__).resolve().parents[1]


def button(app, label):
    return next(b for b in app.button if b.label == label)


def state_get(app, key, default=None):
    return app.session_state[key] if key in app.session_state else default


def banners(app):
    """All rendered HTML, so tests can read the machine-readable status block."""
    return "\n".join(str(getattr(element, "value", "")) for element in app.markdown)


def run_batch(app, timeout=60):
    button(app, "Start batch").click().run()
    deadline = time.time() + timeout
    while app.title[0].value in ("Batch running", "Batch starting", "Batch paused") and time.time() < deadline:
        time.sleep(0.2)
        app.run()
    return app


@pytest.fixture
def studio(monkeypatch, tmp_path):
    import core.run_service as service
    import core.session_manager as sessions
    import streamlit as st

    monkeypatch.setattr(sessions, "session_manager", SessionManager(tmp_path / "sessions"))
    monkeypatch.setattr(service, "score_job_async", AsyncMock(side_effect=AssertionError("Unexpected provider request")))
    monkeypatch.setattr(service, "tailor_resume_async", AsyncMock(return_value=load_owner_profile()))
    monkeypatch.setattr(service, "collect_company_context", Mock(return_value={"sources": [], "warnings": []}))
    monkeypatch.setattr(service, "review_resume_async", AsyncMock(return_value=(
        {"passed": True, "errors": []},
        {"approved": True, "score": 91, "findings": [], "prompt_tokens": 1, "completion_tokens": 1},
    )))

    def render(**kwargs):
        path = Path(kwargs["output_dir"]) / kwargs["custom_filename"]
        path.write_bytes(b"%PDF-test-download")
        return str(path), {"passed": True, "errors": []}

    monkeypatch.setattr(service, "render_and_audit_pdf", render)
    state = {"csv": None}

    def uploader(label, **kwargs):
        data = state["csv"] if label == "Jobs CSV" else None
        if data is None:
            return None
        file = BytesIO(data.encode())
        file.name = "jobs.csv"
        return file

    monkeypatch.setattr(st, "file_uploader", uploader)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    return app, state


def unlock(app):
    app.session_state["owner_unlocked"] = True
    app.run()
    return app


def mock_scores(monkeypatch):
    import core.run_service as scorer

    async def score(job, **kwargs):
        return {
            **job,
            "match_score": 85 if job["company"] != "Groww" else 45,
            "scoring_status": "success",
            "reason": "Profile matches this role.",
            "missing_skills": [],
        }

    monkeypatch.setattr(scorer, "score_job_async", score)


def test_private_studio_shows_no_data_before_unlock(studio):
    app, _ = studio
    app.run()
    assert not app.exception
    assert app.title[0].value == "Owner access only"
    assert any("owner passphrase" in x.label.lower() for x in app.text_input)
    assert not any(b.label == "Start batch" for b in app.button)
    assert not any("Kaustubh" in getattr(x, "value", "") for x in app.markdown)
    assert not any("Kaustubh" in getattr(x, "value", "") for x in app.text)


def test_wrong_passphrase_is_rejected_and_correct_one_unlocks(studio):
    from core.settings import access_key

    app, _ = studio
    app.run()
    app.text_input(key="owner_passphrase").set_value("definitely-not-the-passphrase").run()
    button(app, "Unlock studio").click().run()
    assert not app.exception
    assert any("Incorrect passphrase" in x.value for x in app.error)
    assert app.title[0].value == "Owner access only"

    app.text_input(key="owner_passphrase").set_value(access_key()).run()
    button(app, "Unlock studio").click().run()
    assert not app.exception
    assert any(b.label == "Start batch" for b in app.button)
    assert app.session_state["owner_unlocked"] is True


def test_locked_studio_refuses_to_open_without_configured_passphrase(studio, monkeypatch):
    import core.access

    monkeypatch.setattr(core.access, "access_key", lambda: "")
    app, _ = studio
    app.run()
    assert not app.exception
    assert app.title[0].value == "Studio locked"
    assert any("STUDIO_ACCESS_KEY" in x.value for x in app.error)
    assert not any(b.label == "Start batch" for b in app.button)


def test_upload_screen_requires_a_readable_csv(studio):
    app, state = studio
    unlock(app)
    assert not app.exception
    assert app.title[0].value == "Upload a jobs CSV"
    assert 'data-studio-state="ready"' in banners(app)

    button(app, "Start batch").click().run()
    assert any("Upload a jobs CSV first" in x.value for x in app.error)

    state["csv"] = "wrong,headers\na,b\n"
    app.run()
    assert any("Could not read CSV" in x.value for x in app.error)

    from core.csv_parser import generate_sample_csv_str

    state["csv"] = generate_sample_csv_str()
    app.run()
    assert any("3 jobs loaded" in x.value for x in app.success)
    assert 'data-jobs-total="3"' in banners(app)


def test_profile_is_bundled_locked_and_not_editable(studio):
    app, state = studio
    unlock(app)
    assert not app.exception
    assert app.session_state["profile"]["personal_information"]["name"] == "Kaustubh"
    assert app.session_state["profile"]["personal_information"]["surname"] == "Kale"

    # No customization controls exist anywhere in the app.
    assert not any(getattr(x, "key", None) == "profile_editor" for x in app.text_area)
    assert not any(b.label in ("Apply profile changes", "Load sample profile", "Apply uploaded profile")
                   for b in app.button)
    assert not any(x.label in ("Gemini API keys", "DeepSeek challenger key") for x in app.text_input)
    assert len(app.slider) == 0
    assert not any(x.label in ("Gemini model", "Provider", "AI provider") for x in app.selectbox)

    # Uploading a CSV cannot change the owner identity.
    state["csv"] = "Company,Job Title,Job Description\nAcme,Engineer," + "x" * 80 + "\n"
    app.run()
    assert app.session_state["profile"]["personal_information"]["name"] == "Kaustubh"


def test_batch_runs_and_results_are_ready_to_download(studio, monkeypatch):
    from core.csv_parser import generate_sample_csv_str
    from core.run_store import run_store_instance

    app, state = studio
    state["csv"] = generate_sample_csv_str()
    mock_scores(monkeypatch)
    unlock(app)
    app.run()

    run_batch(app)
    assert not app.exception
    run_id = app.session_state["current_run_id"]
    stages_saved = {event.stage for event in run_store_instance.get_stage_events(run_id)}
    assert {"Score", "Tailor", "Render", "Challenge", "Package"} <= stages_saved
    assert run_store_instance.get_run(run_id)["status"] == "completed"

    assert app.title[0].value == "Batch complete"
    assert counts(app.session_state["batch_results"])["generated"] == 2
    assert app.session_state["zip_bytes"].startswith(b"PK")
    html = banners(app)
    assert 'data-studio-state="complete"' in html
    assert 'data-approved="2"' in html
    assert 'data-zip="1"' in html
    assert any(metric.label == "Jobs processed" for metric in app.metric)

    # The results table stays on screen and one job can be inspected.
    inspector = app.selectbox(key="inspect_job")
    assert len(inspector.options) == 3
    inspector.select(inspector.options[1]).run()
    assert not app.exception


def test_processing_error_state(studio, monkeypatch):
    import core.run_service as scorer
    from core.csv_parser import generate_sample_csv_str

    app, state = studio
    state["csv"] = generate_sample_csv_str()
    monkeypatch.setattr(scorer, "score_job_async", AsyncMock(side_effect=RuntimeError("Simulated scoring failure")))
    unlock(app)
    app.run()
    run_batch(app)
    assert not app.exception
    assert app.title[0].value == "Batch failed"
    assert any("Batch interrupted" in x.value for x in app.error)
    assert 'data-studio-state="failed"' in banners(app)
    assert any(b.label == "Resume batch" for b in app.button)


def test_start_a_new_batch_resets_the_workspace(studio, monkeypatch):
    from core.csv_parser import generate_sample_csv_str

    app, state = studio
    state["csv"] = generate_sample_csv_str()
    mock_scores(monkeypatch)
    unlock(app)
    app.run()
    run_batch(app)

    button(app, "Start a new batch").click().run()
    assert not app.exception
    assert app.title[0].value == "Upload a jobs CSV"
    assert state_get(app, "batch_results") is None


def test_recovery_lookup_finds_and_loads_a_run(studio, monkeypatch):
    from core.csv_parser import generate_sample_csv_str
    from core.run_store import run_store_instance

    app, state = studio
    state["csv"] = generate_sample_csv_str()
    mock_scores(monkeypatch)
    unlock(app)
    app.run()
    run_batch(app)

    run_id = state_get(app, "current_run_id")
    recovery_code = state_get(app, "current_recovery_code")
    assert run_store_instance.verify_recovery(run_id, recovery_code)

    app.text_input(key="recovery_code_input").set_value(recovery_code).run()
    button(app, "Find run").click().run()
    assert not app.exception
    assert any(f"Run {run_id} found" in x.value for x in app.success)

    button(app, "Load these results").click().run()
    assert not app.exception
    assert app.title[0].value == "Batch complete"


def test_lock_studio_clears_the_session(studio):
    app, _ = studio
    unlock(app)
    button(app, "Lock studio").click().run()
    assert not app.exception
    assert not state_get(app, "owner_unlocked", False)
    assert app.title[0].value == "Owner access only"


def test_status_banner_is_machine_readable():
    banner = status_banner(
        "running",
        '3 of 9 jobs scored - <script>bad()</script>',
        totals={
            "total": 9,
            "scored": 3,
            "approved": 1,
            "zip": True,
            "run_id": "run_demo",
            "stage": "Tailor",
            "recovery_code": "jh-demo-0000-0000",
        },
    )
    assert 'data-studio-state="running"' in banner
    assert 'data-jobs-total="9"' in banner
    assert 'data-jobs-scored="3"' in banner
    assert 'data-approved="1"' in banner
    assert 'data-zip="1"' in banner
    assert 'data-run-id="run_demo"' in banner
    assert 'data-stage="Tailor"' in banner
    assert 'data-recovery-code="jh-demo-0000-0000"' in banner
    assert "RUNNING" in banner
    assert "<script>" not in banner
    assert "&lt;script&gt;" in banner


def test_result_groups_and_filters():
    results = [
        {"company": "Acme", "status": "Render Error", "match_score": 80},
        {"company": "Beta", "status": "Below Threshold", "match_score": 45},
        {"company": "Gamma", "pdf_path": "resume.pdf", "review_status": "Approved", "scoring_status": "success"},
    ]
    assert len(filter_results(results, "Needs attention")) == 1
    assert len(filter_results(results, "Approved")) == 1
    assert len(filter_results(results, "Below Threshold")) == 1
    assert filter_results(results) == results
    assert display_status(results[0]) == "Render Error"


