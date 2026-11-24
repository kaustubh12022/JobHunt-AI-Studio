
import asyncio
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import core.run_service as workers
from core.ai_errors import CredentialsRequired
from core.models import JobInput, RunConfig
from core.resume_review import REVIEW_WEIGHTS, _parse_review, approved_pdf, inspect_pdf
from core.run_store import RunStore


def review_json(score, findings=()):
    return json.dumps({"scores": dict.fromkeys(REVIEW_WEIGHTS, score), "findings": list(findings)})


def test_approval_requires_audited_pdf_and_supported_mandatory_qualifications():
    assert _parse_review(review_json(85), {"passed": True}, 85, 1, 2)["approved"]
    assert not _parse_review(review_json(100), {"passed": False}, 85, 1, 2)["approved"]
    finding = {
        "severity": "unsupported_requirement", "section": "Requirements",
        "evidence": "The required license is absent from the profile.", "correction": "Request proof.",
    }
    assert not _parse_review(review_json(100, [finding]), {"passed": True}, 85, 1, 2)["approved"]
    with pytest.raises(ValueError):
        _parse_review('{"scores": {}}', {"passed": True}, 85, 1, 2)


def test_resume_review_reuses_draft_after_provider_failure_and_excludes_changed_pdf(tmp_path, monkeypatch):
    service = workers.RunService(RunStore(tmp_path))
    job = JobInput("j1", "Acme", "Engineer", "The role requires verified software engineering experience.")
    config = RunConfig("reviewed", "deepseek", "deepseek-flash", reviewer_model="deepseek-flash")
    run_id, code = service.create_run(config, {"personal_information": {"name": "Candidate"}}, [job])
    score = AsyncMock(return_value={**job.to_dict(), "scoring_status": "success", "match_score": 89})
    tailor = AsyncMock(side_effect=[{"profile_summary": "Draft one"}, {"profile_summary": "Draft two"}])
    monkeypatch.setattr(workers, "score_job_async", score)
    monkeypatch.setattr(workers, "tailor_resume_async", tailor)
    monkeypatch.setattr(workers, "collect_company_context", lambda job: {"sources": [], "warnings": ["Unavailable"]})

    def render(tailored_resume, output_dir, job, custom_filename):
        path = output_dir / custom_filename
        path.write_text(tailored_resume["profile_summary"], encoding="utf-8")
        return str(path), {"passed": True}

    monkeypatch.setattr(workers, "render_and_audit_pdf", render)
    finding = {"severity": "suggestion", "section": "Summary", "evidence": "Too broad", "correction": "Focus on APIs"}
    first = _parse_review(review_json(72, [finding]), {"passed": True}, 85, 10, 3)
    second = _parse_review(review_json(91), {"passed": True}, 85, 11, 4)
    reviewer = AsyncMock(side_effect=[CredentialsRequired("Replace DeepSeek key"), ({"passed": True}, first),
                                     ({"passed": True}, second)])
    monkeypatch.setattr(workers, "review_resume_async", reviewer)

    service.start_or_resume_run(run_id, code, background=False)
    assert service.get_run_status(run_id)["status"] == "awaiting_credentials"
    intermediate = service.store.get_completed_job_results(run_id)[0]
    assert len(intermediate.attempts) == 1
    assert not intermediate.pdf_path
    assert Path(intermediate.draft_path).is_file()

    service.start_or_resume_run(run_id, code, background=False)
    result = service.store.get_completed_job_results(run_id)[0]
    assert result.review_status == "Approved"
    assert len(result.attempts) == 2
    assert result.challenger_score == 91
    assert (result.prompt_tokens, result.completion_tokens) == (21, 7)
    score.assert_awaited_once()
    assert tailor.await_args.kwargs["review_feedback"]["review"]["findings"] == [finding]
    assert approved_pdf(result.to_dict())
    bundle = service.package_run_artifacts(run_id, code, persist=False)
    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes)) as archive:
        assert [name for name in archive.namelist() if name.startswith("resumes/")] == [
            "resumes/" + Path(result.pdf_path).name
        ]
    Path(result.pdf_path).write_bytes(b"modified")
    assert not approved_pdf(result.to_dict())
    assert service.package_run_artifacts(run_id, code, persist=False).verified_count == 0


def test_local_pdf_check_reads_real_resume_and_detects_missing_expected_text(tmp_path):
    from core.pdf_generator import render_and_audit_pdf
    from core.profile_manager import load_profile_from_file

    profile = load_profile_from_file(Path(__file__).resolve().parents[1] / "data" / "profile.yaml")
    path, audit = render_and_audit_pdf(profile, tmp_path)
    text, image, checked = inspect_pdf(path, profile, audit)
    assert checked["passed"], checked
    assert text.strip() and image
    profile["personal_information"]["email"] = "missing@example.com"
    _, _, checked = inspect_pdf(path, profile, audit)
    assert not checked["passed"]
    assert "Expected email is missing from the PDF text." in checked["errors"]



def test_company_lookup_blocks_non_public_destinations(monkeypatch):
    import time

    import core.company_context as company

    monkeypatch.setattr(company.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 80))])
    with pytest.raises(ValueError, match="public unicast"):
        company._fetch("https://company.example", time.monotonic() + 5)
    context = company.collect_company_context({"url": "https://company.example"})
    assert not context["sources"]
    assert any("unavailable" in warning for warning in context["warnings"])
