"""
Verification Suite: Provider Reliability, Model Validation & Failure Correctness.
Tests:
- Strict model catalog validation forbidding silent model substitution
- Handling of AI failures: no fabricated 60% fallback scores or silent master profile fallbacks
- ScopedAIClient isolation: process-wide environment variables remain unaffected
- Cost estimator accuracy with dated pricing and handling of unknown models
- Rate limiter cooldown preservation
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from core.ai_engine import ScopedAIClient, extract_json_str
from core.cost_estimator import estimate_batch_cost
from core.models_catalog import validate_model, get_supported_models, normalize_provider_name
from core.scorer import score_job_async
from core.resume_tailor import tailor_resume_async


def test_model_catalog_strict_validation():
    """Verify that unsupported models raise ValueError and prevent silent substitution."""
    # Valid models
    assert validate_model("gemini", "gemini-3.5-flash-lite") == "gemini-3.5-flash-lite"
    assert validate_model("deepseek", "deepseek-chat") == "deepseek-chat"
    assert validate_model("openai", "gpt-4o-mini") == "gpt-4o-mini"

    # Default model resolution when model is empty
    assert validate_model("gemini", "") == "gemini-3.5-flash-lite"
    assert validate_model("deepseek", None) == "deepseek-chat"

    # Invalid models MUST raise ValueError
    with pytest.raises(ValueError) as exc:
        validate_model("gemini", "invented-gemini-model-999")
    assert "Invalid model" in str(exc.value)
    assert "Silent model substitution is disabled" in str(exc.value)

    # Provider mismatch MUST raise ValueError
    with pytest.raises(ValueError) as exc:
        validate_model("gemini", "gpt-4o-mini")
    assert "belongs to provider 'openai'" in str(exc.value)


def test_ai_scoring_failure_reports_failed_stage_truthfully():
    """Verify that an AI failure during scoring results in a failed stage, NOT a fake 60% score."""
    job = {
        "job_id": "j_fail",
        "company": "Acme",
        "title": "Engineer",
        "jd_text": "Need python dev.",
    }
    profile = {"personal_information": {"name": "Test"}}

    # Mock an error from LLM
    mock_client = AsyncMock()
    mock_client.call_llm.side_effect = RuntimeError("API connection timeout")

    import asyncio
    scored = asyncio.run(score_job_async(job, profile, ai_client=mock_client))

    # Truthful failure reporting
    assert scored["scoring_status"] == "failed"
    assert scored["match_score"] == 0
    assert "Scoring failed" in scored["reason"]
    assert "Scoring Error" in scored["status"]
    # Verify no fabricated 60% score
    assert scored["match_score"] != 60


def test_ai_tailoring_failure_raises_or_fails_without_silent_fallback():
    """Verify that an AI failure during tailoring raises or flags failure without silent fallback."""
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

    import asyncio
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(tailor_resume_async(job, profile, ai_client=mock_client))
    assert "Resume tailoring failed" in str(exc.value)


def test_cost_estimator_truthful_and_unknown_handling():
    """Verify that unknown models report 'Unknown pricing' rather than free."""
    # Unknown model
    unknown_est = estimate_batch_cost(
        total_jobs=20,
        provider="custom_llm",
        model="custom-32b",
        is_gemini_free=False,
    )
    assert unknown_est["cost_usd"] is None
    assert unknown_est["cost_display"] == "Unknown pricing"
    assert unknown_est["is_free"] is False

    # Gemini Lite is NOT free if paid tier is selected
    paid_lite = estimate_batch_cost(
        total_jobs=100,
        provider="gemini",
        model="gemini-3.5-flash-lite",
        is_gemini_free=False,
    )
    assert paid_lite["cost_usd"] > 0
    assert paid_lite["is_free"] is False

    # Gemini Free Tier is explicitly $0.00
    free_lite = estimate_batch_cost(
        total_jobs=100,
        provider="gemini",
        model="gemini-3.5-flash-lite",
        is_gemini_free=True,
    )
    assert free_lite["cost_usd"] == 0.0
    assert free_lite["is_free"] is True


def test_scoped_client_credential_isolation():
    """Verify that ScopedAIClient does not mutate process environment variables."""
    initial_openai = os.environ.get("OPENAI_API_KEY")
    initial_deepseek = os.environ.get("DEEPSEEK_API_KEY")

    client = ScopedAIClient(
        provider="openai",
        api_keys=["test-key-abc-123"],
        model="gpt-4o-mini",
    )
    assert client.api_keys == ["test-key-abc-123"]

    # Global environment must remain untouched
    assert os.environ.get("OPENAI_API_KEY") == initial_openai
    assert os.environ.get("DEEPSEEK_API_KEY") == initial_deepseek


def test_gemini_key_pool_multi_project_rotation_and_isolated_cooldown():
    """Verify that GeminiKeyPool gives each key an independent rate limiter and isolated cooldown."""
    import asyncio
    from core.ai_engine import GeminiKeyPool

    pool = GeminiKeyPool(keys=["key_proj_alpha_12345", "key_proj_beta_67890"], rpm_limit=15)
    assert len(pool.get_keys()) == 2

    # First acquisition gets first key
    _, key1, mask1 = asyncio.run(pool.acquire_client())
    assert key1 == "key_proj_alpha_12345"

    # Second acquisition immediately gets second key (round robin with 0 wait)
    _, key2, mask2 = asyncio.run(pool.acquire_client())
    assert key2 == "key_proj_beta_67890"

    # Now rate limit key 1
    pool.record_rate_limit("key_proj_alpha_12345", cooldown_seconds=60.0)

    # Next acquisition MUST select key 2 because key 1 is on cooldown
    _, key3, _ = asyncio.run(pool.acquire_client())
    assert key3 == "key_proj_beta_67890"

    # Stats tracked per key
    assert pool.stats[mask1]["calls"] == 1
    assert pool.stats[mask1]["rate_limits"] == 1
    assert pool.stats[mask2]["calls"] == 2
    assert pool.stats[mask2]["rate_limits"] == 0
