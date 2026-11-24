"""
Verification Suite: DeepSeek Provider, Locked Model Catalog & Failure Correctness.
Tests:
- Strict DeepSeek-only model catalog forbidding silent substitution
- Truthful scoring/tailoring failures (no fabricated scores or silent fallbacks)
- Token estimation with and without operator-supplied pricing
- Credential isolation and actionable 401 handling
- Bounded retry of transient provider errors
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
import core.ai_engine as engine
from core.ai_errors import CredentialsRequired
from core.cost_estimator import estimate_batch_cost
from core.models_catalog import validate_model, get_supported_models, normalize_provider_name
from core.resume_tailor import tailor_resume_async
from core.scorer import score_job_async


def test_model_catalog_is_locked_to_deepseek():
    assert normalize_provider_name("deepseek") == "deepseek"
    assert validate_model("deepseek", "deepseek-flash") == "deepseek-flash"
    assert validate_model("deepseek", "") == "deepseek-flash"
    assert get_supported_models("deepseek") == ["deepseek-flash"]

    with pytest.raises(ValueError, match="locked to DeepSeek"):
        normalize_provider_name("gemini")
    with pytest.raises(ValueError, match="locked to DeepSeek"):
        normalize_provider_name("openai")
    with pytest.raises(ValueError) as exc:
        validate_model("deepseek", "gemini-3.5-flash-lite")
    assert "Invalid model" in str(exc.value)
    assert "Silent model substitution is disabled" in str(exc.value)


def test_ai_scoring_failure_reports_failed_stage_truthfully():
    job = {"job_id": "j_fail", "company": "Acme", "title": "Engineer", "jd_text": "Need python dev."}
    profile = {"personal_information": {"name": "Test"}}

    mock_client = AsyncMock()
    mock_client.call_llm.side_effect = RuntimeError("API connection timeout")

    scored = asyncio.run(score_job_async(job, profile, ai_client=mock_client))
    assert scored["scoring_status"] == "failed"
    assert scored["match_score"] == 0
    assert "Scoring failed" in scored["reason"]
    assert "Scoring Error" in scored["status"]
    assert scored["match_score"] != 60


def test_ai_tailoring_failure_raises_without_silent_fallback():
    job = {
        "job_id": "j_tailor_fail",
        "company": "Beta Corp",
        "title": "Software Engineer",
        "jd_text": "Need experienced Java developer.",
        "match_score": 85,
    }
    profile = {"personal_information": {"name": "Test"}, "skills": ["Java", "SQL"]}

    mock_client = AsyncMock()
    mock_client.call_llm.side_effect = RuntimeError("Rate limit exceeded")

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(tailor_resume_async(job, profile, ai_client=mock_client))
    assert "Resume tailoring failed" in str(exc.value)


def test_cost_estimator_is_token_only_until_operator_sets_pricing(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_INPUT_PER_M", raising=False)
    monkeypatch.delenv("DEEPSEEK_OUTPUT_PER_M", raising=False)
    estimate = estimate_batch_cost(total_jobs=50, max_resumes=20)
    assert estimate["total_tokens"] > 50_000
    assert estimate["cost_usd"] is None
    assert estimate["cost_display"] == "Token estimate only"
    assert estimate["is_free"] is False

    monkeypatch.setenv("DEEPSEEK_INPUT_PER_M", "0.27")
    monkeypatch.setenv("DEEPSEEK_OUTPUT_PER_M", "1.10")
    priced = estimate_batch_cost(total_jobs=50, max_resumes=20)
    assert priced["cost_usd"] > 0
    assert priced["cost_display"].startswith("~$")


def test_client_requires_a_key_and_does_not_mutate_environment(monkeypatch):
    initial = os.environ.get("DEEPSEEK_API_KEY")

    monkeypatch.setattr(engine, "deepseek_api_key", lambda: "")
    with pytest.raises(CredentialsRequired, match="not configured"):
        engine.DeepSeekClient()

    monkeypatch.setattr(engine, "deepseek_api_key", lambda: "test-key-abc-123")
    client = engine.DeepSeekClient()
    assert client.api_key == "test-key-abc-123"
    assert os.environ.get("DEEPSEEK_API_KEY") == initial
    asyncio.run(client.close())


def _fake_transport(monkeypatch, script):
    """Replace AsyncOpenAI with a scripted transport.

    `script` holds HTTP status codes to raise, or "ok" to answer successfully.
    Returns the recorded calls and the exception class used for failures.
    """
    calls = []

    class ProviderError(Exception):
        def __init__(self, status):
            self.status_code = status
            self.response = SimpleNamespace(headers={"retry-after": "0"})

    class Transport:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            calls.append(kwargs)
            outcome = script.pop(0)
            if outcome != "ok":
                raise ProviderError(outcome)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],
                usage=SimpleNamespace(prompt_tokens=42, completion_tokens=5),
            )

        async def close(self):
            return None

    monkeypatch.setattr(engine, "AsyncOpenAI", Transport)
    return calls, ProviderError


def test_deepseek_retries_transient_errors_then_succeeds(monkeypatch):
    calls, ProviderError = _fake_transport(monkeypatch, [503, 429, "ok"])
    monkeypatch.setattr(engine, "deepseek_api_key", lambda: "test-key")
    client = engine.DeepSeekClient(max_attempts=3)

    result = asyncio.run(client.call_llm("system", "job"))
    assert result == ('{"ok":true}', 42, 5)
    assert len(calls) == 3
    asyncio.run(client.close())


def test_deepseek_credential_errors_are_actionable(monkeypatch):
    _, ProviderError = _fake_transport(monkeypatch, [401])
    monkeypatch.setattr(engine, "deepseek_api_key", lambda: "bad-key")
    client = engine.DeepSeekClient(max_attempts=2)

    with pytest.raises(CredentialsRequired, match="rejected"):
        asyncio.run(client.call_llm("system", "job"))
    asyncio.run(client.close())


def test_deepseek_non_retryable_client_errors_fail_fast(monkeypatch):
    calls, ProviderError = _fake_transport(monkeypatch, [400])
    monkeypatch.setattr(engine, "deepseek_api_key", lambda: "test-key")
    client = engine.DeepSeekClient(max_attempts=3)

    with pytest.raises(RuntimeError, match="HTTP 400"):
        asyncio.run(client.call_llm("system", "job"))
    assert len(calls) == 1
    asyncio.run(client.close())
