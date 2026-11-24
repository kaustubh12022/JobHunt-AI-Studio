"""Locked-studio UI behavior checks with isolated files and no live provider requests."""
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from streamlit.testing.v1 import AppTest

from core.profile_manager import load_owner_profile
from core.session_manager import SessionManager
from ui import counts, filter_results, pipeline_html

ROOT = Path(__file__).resolve().parents[1]


def button(app, label):
    return next(b for b in app.button if b.label == label)


def state_get(app, key, default=None):
    return app.session_state[key] if key in app.session_state else default


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
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
    return app, state


def unlock(app):
    app.session_state["owner_unlocked"] = True
    app.run()
    return app


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


def test_setup_missing_inputs_and_invalid_csv(studio):
    app, state = studio
    unlock(app)
    assert not app.exception
    assert button(app, "Start batch").disabled
    assert any("upload a valid jobs CSV" in x.value for x in app.caption)
    state["csv"] = "wrong,headers\na,b\n"
    app.run()
    assert not app.exception
    assert any("Could not read CSV" in x.value for x in app.error)
    assert button(app, "Start batch").disabled


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


def test_mock_pipeline_results_filters_downloads_and_clear(studio, monkeypatch):
    from core.csv_parser import generate_sample_csv_str
    import core.run_service as scorer

    app, state = studio
    state["csv"] = generate_sample_csv_str()

    async def score(job, **kwargs):
        return {**job, "match_score": 85 if job["company"] != "Groww" else 45,
                "scoring_status": "success", "reason": "Profile matches this role.", "missing_skills": []}

    monkeypatch.setattr(scorer, "score_job_async", score)
    unlock(app)
    app.run()
    button(app, "Start batch").click().run()
    assert not app.exception
    from core.run_store import run_store_instance
    run_id = app.session_state["current_run_id"]
    stages_saved = {event.stage for event in run_store_instance.get_stage_events(run_id)}
    assert {"Score", "Tailor", "Render", "Challenge", "Package"} <= stages_saved
    assert run_store_instance.get_run(run_id)["status"] == "completed"
    assert counts(app.session_state["batch_results"]) == {"processed": 3, "generated": 2, "skipped": 1, "attention": 0}
    assert app.session_state["zip_bytes"].startswith(b"PK")
    assert app.session_state["pipeline_progress"] == 1
    assert "BATCH COMPLETE" in app.session_state["pipeline_view"]
    app.text_input(key="result_query").set_value("nothing matches").run()
    assert any("No jobs match" in x.value for x in app.info)
    app.text_input(key="result_query").set_value("").run()
    app.selectbox(key="result_group").select("Skipped").run()
    assert any("Showing 1 of 3" in x.value for x in app.caption)
    button(app, "Clear results").click().run()
    assert not app.exception
    assert app.session_state["batch_results"] is None
    assert any("Your results will appear here" in x.value for x in app.markdown)


def test_processing_error_state(studio, monkeypatch):
    import core.run_service as scorer
    from core.csv_parser import generate_sample_csv_str
    app, state = studio
    state["csv"] = generate_sample_csv_str()
    monkeypatch.setattr(scorer, "score_job_async", AsyncMock(side_effect=RuntimeError("Simulated scoring failure")))
    unlock(app)
    app.run()
    button(app, "Start batch").click().run()
    assert not app.exception
    assert any("Batch interrupted" in x.value for x in app.error)
    assert 'pipeline-step failed' in app.session_state["pipeline_view"]


def test_pipeline_escapes_inputs_and_filters_attention():
    from ui import render_ats_checklist, render_health_card

    html = pipeline_html("Scoring", '<img src=x onerror="alert(1)">', "Engineer")
    assert "<img" not in html
    assert "&lt;img" in html
    results = [{"company": "Acme", "status": "Render Error", "match_score": 80},
               {"company": "Beta", "status": "Below Threshold", "match_score": 45},
               {"company": "Gamma", "pdf_path": "resume.pdf", "scoring_status": "fallback"}]
    assert len(filter_results(results, "", "Needs attention")) == 2
    assert filter_results(results, "BETA", "Skipped") == [results[1]]

    health_html = render_health_card({"status": "healthy", "checks": {"storage": {"free_space_mb": 500}}})
    assert "System Health" in health_html
    assert "healthy" in health_html

    ats_html = render_ats_checklist({"passed": True, "page_count": 1, "errors": [], "warnings": []})
    assert "Universal ATS Compliance Passed" in ats_html
    assert "Strict 1-Page Constraint" in ats_html


def test_sidebar_recovery_lookup_and_run_store_integration(studio, monkeypatch):
    from core.csv_parser import generate_sample_csv_str
    from core.run_store import run_store_instance
    import core.run_service as scorer

    app, state = studio
    state["csv"] = generate_sample_csv_str()

    async def score(job, **kwargs):
        return {
            **job,
            "match_score": 85 if job["company"] != "Groww" else 45,
            "scoring_status": "success",
            "reason": "Match",
            "missing_skills": [],
        }

    monkeypatch.setattr(scorer, "score_job_async", score)
    unlock(app)
    app.run()
    button(app, "Start batch").click().run()
    assert not app.exception

    rec_code = state_get(app, "current_recovery_code")
    run_id = state_get(app, "current_run_id")
    assert rec_code is not None
    assert run_id is not None
    assert run_store_instance.verify_recovery(run_id, rec_code)

    app.text_input(key="sidebar_recovery_code").set_value(rec_code).run()
    button(app, "Inspect run").click().run()
    assert not app.exception
    assert any(f"Run {run_id} found" in s.value for s in app.success)

    app.selectbox(key="result_group").select("Approved").run()
    assert any("Showing 2 of 3" in x.value for x in app.caption)
    app.selectbox(key="result_group").select("Below Threshold").run()
    assert any("Showing 1 of 3" in x.value for x in app.caption)


def test_lock_studio_clears_the_session(studio):
    app, _ = studio
    unlock(app)
    button(app, "Lock studio").click().run()
    assert not app.exception
    assert not state_get(app, "owner_unlocked", False)
    assert app.title[0].value == "Owner access only"
