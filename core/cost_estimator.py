"""
JobHunt AI Studio - API Cost & Token Estimator
Calculates token projections and API cost projections based on actual input sizes,
explicit billing tiers, and dated catalog pricing.
Gracefully handles unknown model pricing without guessing or crashing.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.models_catalog import get_model_spec, normalize_provider_name


def estimate_batch_cost(
    total_jobs: int,
    provider: str,
    model: str,
    is_gemini_free: bool = False,
    max_resumes: int = 20,
    expected_shortlist_rate: float = 0.50,
    avg_jd_chars: int = 2000,
    profile_chars: int = 3500,
) -> Dict[str, Any]:
    """
    Estimates total token usage and API cost before starting a batch run.
    Uses actual input character sizing and explicit billing tier selection.
    """
    if total_jobs <= 0:
        return {
            "total_jobs": 0,
            "estimated_tailored": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "is_free": True,
            "cost_display": "$0.00",
            "tier_name": "None",
        }

    try:
        norm_prov = normalize_provider_name(provider)
    except Exception:
        norm_prov = "unknown"

    spec = get_model_spec(model)

    # 1. Token Estimation from Character Sizes (~4 chars per token)
    scoring_jd_tokens = max(200, avg_jd_chars // 4)
    scoring_prompt_overhead = 500
    scoring_input_tokens = total_jobs * (scoring_jd_tokens + scoring_prompt_overhead)
    scoring_output_tokens = total_jobs * 160

    estimated_tailored = min(max_resumes, max(1, int(total_jobs * expected_shortlist_rate)))
    tailoring_input_tokens = estimated_tailored * (profile_chars // 4 + scoring_jd_tokens + 400)
    tailoring_output_tokens = estimated_tailored * 850

    total_input = scoring_input_tokens + tailoring_input_tokens
    total_output = scoring_output_tokens + tailoring_output_tokens
    total_tokens = total_input + total_output

    # 2. Pricing Determination
    if norm_prov == "gemini" and is_gemini_free:
        return {
            "total_jobs": total_jobs,
            "estimated_tailored": estimated_tailored,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_tokens,
            "cost_usd": 0.0,
            "is_free": True,
            "cost_display": "🎉 $0.00 (Gemini Free Tier — 15 RPM)",
            "tier_name": "Google Gemini (Free Tier)",
        }

    if spec is None:
        # Unknown pricing: do not infer free
        return {
            "total_jobs": total_jobs,
            "estimated_tailored": estimated_tailored,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_tokens,
            "cost_usd": None,
            "is_free": False,
            "cost_display": "Unknown pricing",
            "tier_name": f"{provider} ({model}) - Custom",
        }

    input_cost = (total_input / 1_000_000) * spec["input_per_m"]
    output_cost = (total_output / 1_000_000) * spec["output_per_m"]
    cost_usd = round(input_cost + output_cost, 5)

    return {
        "total_jobs": total_jobs,
        "estimated_tailored": estimated_tailored,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "is_free": False,
        "cost_display": f"~${cost_usd:.4f} USD",
        "tier_name": spec.get("display_name", model),
    }
