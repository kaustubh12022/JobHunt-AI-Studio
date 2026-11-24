"""
JobHunt AI Studio - Batch Load Estimator
Projects token usage for a batch from actual input sizes. USD pricing is only
reported when the operator supplies server-side per-million rates; otherwise the
studio shows a token-only estimate instead of inventing a price.
"""

from __future__ import annotations

from typing import Any, Dict

from core.settings import (
    input_price_per_million,
    output_price_per_million,
)


def estimate_batch_cost(
    total_jobs: int,
    max_resumes: int = 20,
    expected_shortlist_rate: float = 0.50,
    avg_jd_chars: int = 2000,
    profile_chars: int = 4000,
) -> Dict[str, Any]:
    """Estimate scoring, drafting and review tokens for a batch."""
    if total_jobs <= 0:
        return {
            "total_jobs": 0,
            "estimated_tailored": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "is_free": False,
            "cost_display": "No jobs",
        }

    # ~4 characters per token for English text and JSON payloads.
    scoring_jd_tokens = max(200, avg_jd_chars // 4)
    scoring_input_tokens = total_jobs * (scoring_jd_tokens + 700)
    scoring_output_tokens = total_jobs * 180

    estimated_tailored = min(max_resumes, max(1, int(total_jobs * expected_shortlist_rate)))
    tailoring_input_tokens = estimated_tailored * (profile_chars // 4 + scoring_jd_tokens + 500)
    tailoring_output_tokens = estimated_tailored * 1100

    # The reviewer receives PDF text, a page image and the source evidence.
    review_rounds = 2
    review_input_tokens = estimated_tailored * review_rounds * 4500
    review_output_tokens = estimated_tailored * review_rounds * 700

    total_input = scoring_input_tokens + tailoring_input_tokens + review_input_tokens
    total_output = scoring_output_tokens + tailoring_output_tokens + review_output_tokens

    input_price = input_price_per_million()
    output_price = output_price_per_million()
    if input_price is None or output_price is None:
        cost_usd = None
        cost_display = "Token estimate only"
    else:
        cost_usd = round(
            (total_input / 1_000_000) * input_price + (total_output / 1_000_000) * output_price,
            5,
        )
        cost_display = f"~${cost_usd:.4f} USD"

    return {
        "total_jobs": total_jobs,
        "estimated_tailored": estimated_tailored,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "cost_usd": cost_usd,
        "is_free": False,
        "cost_display": cost_display,
    }
