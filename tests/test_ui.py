"""UI behavior checks with isolated files and no live provider requests."""
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from streamlit.testing.v1 import AppTest

from core.profile_manager import DEFAULT_PROFILE_TEMPLATE, normalize_profile, profile_to_yaml_str
from core.session_manager import SessionManager
from ui import counts, filter_results, pipeline_html

ROOT = Path(__file__).resolve().parents[1]


def button(app, label):
    return next(b for b in app.button if b.label == label)


@pytest.fixture
def studio(monkeypatch, tmp_path):
    import core.ai_engine as engine
    import core.session_manager as sessions
    import core.scorer as scorer
    import core.resume_tailor as tailor
    import core.pdf_generator as pdf
    import streamlit as st

    for key in ("GEMINI_API_KEYS", "GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(engine, "ai_engine_instance", Mock())
    monkeypatch.setattr(sessions, "session_manager", SessionManager(tmp_path / "sessions"))
    monkeypatch.setattr(scorer, "score_job_async", AsyncMock(side_effect=AssertionError("Unexpected provider request")))
    monkeypatch.setattr(tailor, "tailor_resume_async", AsyncMock(return_value=normalize_profile(DEFAULT_PROFILE_TEMPLATE)))

    def render(**kwargs):
        path = Path(kwargs["output_dir"]) / f"{kwargs['job']['job_id']}.pdf"
        path.write_bytes(b"%PDF-test-download")
        return str(path)

    monkeypatch.setattr(pdf, "generate_single_page_pdf", render)
    state = {"csv": None, "profile": None}

    def uploader(label, **kwargs):
        data = state["csv" if label == "Jobs CSV" else "profile"]
        if data is None:
            return None
        file = BytesIO(data.encode())
        file.name = "jobs.csv" if label == "Jobs CSV" else "profile.yaml"
        return file

    monkeypatch.setattr(st, "file_uploader", uploader)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
    return app, state


def test_setup_missing_inputs_and_invalid_csv(studio):
    app, state = studio
    app.run()
    assert not app.exception
    assert button(app, "Start batch").disabled
    assert any("upload a valid jobs CSV" in x.value for x in app.caption)
    assert not any("ATS Audit Pass" in x.value for x in app.markdown)
    state["csv"] = "wrong,headers\na,b\n"
    app.run()
    assert not app.exception
    assert any("Could not read CSV" in x.value for x in app.error)
    assert button(app, "Start batch").disabled


def test_profile_apply_edit_and_sample(studio):
    app, state = studio
    app.run()
    before = app.session_state["profile"]["personal_information"]["name"]
    state["profile"] = 'personal_information:\n  name: Taylor\n  email: taylor@example.com\n'
    app.run()
    assert app.session_state["profile"]["personal_information"]["name"] == before
    button(app, "Apply uploaded profile").click().run()
    assert not app.exception
    assert app.session_state["profile"]["personal_information"]["name"] == "Taylor"
    assert "Taylor" in app.text_area(key="profile_editor").value
    app.text_area(key="profile_editor").set_value('personal_information:\n  name: Jordan\n')
    button(app, "Apply profile changes").click().run()
    assert not app.exception
    assert app.session_state["profile"]["personal_information"]["name"] == "Jordan"
    button(app, "Load sample profile").click().run()
    assert not app.exception
    assert app.session_state["profile"]["personal_information"]["name"] == before
    app.text_area(key="profile_editor").set_value('[not: valid')
    button(app, "Apply profile changes").click().run()
    assert any("Could not apply changes" in x.value for x in app.error)


def test_mock_pipeline_results_filters_downloads_and_clear(studio, monkeypatch):
    from core.csv_parser import generate_sample_csv_str
    import core.scorer as scorer
    import ui

    app, state = studio
    state["csv"] = generate_sample_csv_str()
    stages = []
    original = ui.pipeline_html

    def trace(stage=None, *args, **kwargs):
        stages.append(stage)
        return original(stage, *args, **kwargs)

    monkeypatch.setattr(ui, "pipeline_html", trace)
    async def score(job, **kwargs):
        return {**job, "match_score": 85 if job["company"] != "Groww" else 45,
                "scoring_status": "success", "reason": "Profile matches this role.", "missing_skills": []}
    monkeypatch.setattr(scorer, "score_job_async", score)
    app.run()
    next(x for x in app.text_input if x.label == "Gemini API keys").set_value("ui-test-only-key").run()
    button(app, "Start batch").click().run()
    assert not app.exception
    assert all(stage in stages for stage in ("Scoring", "Tailoring", "PDF generation", "Packaging"))
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
    import core.scorer as scorer
    from core.csv_parser import generate_sample_csv_str
    app, state = studio
    state["csv"] = generate_sample_csv_str()
    monkeypatch.setattr(scorer, "score_job_async", AsyncMock(side_effect=RuntimeError("Simulated scoring failure")))
    app.run()
    next(x for x in app.text_input if x.label == "Gemini API keys").set_value("ui-test-only-key").run()
    button(app, "Start batch").click().run()
    assert not app.exception
    assert any("Batch interrupted" in x.value for x in app.error)
    assert 'pipeline-step failed' in app.session_state["pipeline_view"]


def test_pipeline_escapes_inputs_and_filters_attention():
    html = pipeline_html("Scoring", '<img src=x onerror="alert(1)">', "Engineer")
    assert "<img" not in html
    assert "&lt;img" in html
    results = [{"company": "Acme", "status": "Render Error", "match_score": 80},
               {"company": "Beta", "status": "Below Threshold", "match_score": 45},
               {"company": "Gamma", "pdf_path": "resume.pdf", "scoring_status": "fallback"}]
    assert len(filter_results(results, "", "Needs attention")) == 2
    assert filter_results(results, "BETA", "Skipped") == [results[1]]
