"""
JobHunt AI Studio - Locked Model Catalog
The studio runs on a single DeepSeek model. Silent substitution is forbidden.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

SUPPORTED_PROVIDERS = ("deepseek",)

DEFAULT_MODELS: Dict[str, str] = {
    "deepseek": "deepseek-flash",
}

MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    "deepseek-flash": {
        "provider": "deepseek",
        "display_name": "DeepSeek V4.1 Flash",
        "context_window": 1_048_576,
        "max_output_tokens": 393_216,
        "input_modalities": ("text", "image"),
        "default_effort": "high",
        "is_default": True,
    },
}


def normalize_provider_name(provider_str: str) -> str:
    p = (provider_str or "").strip().lower()
    if p in ("deepseek", "deepseek-flash", "deepseek v4.1 flash"):
        return "deepseek"
    raise ValueError(
        f"Unsupported AI provider '{provider_str}'. This studio is locked to DeepSeek."
    )


def get_supported_models(provider: str) -> List[str]:
    norm_p = normalize_provider_name(provider)
    return [m for m, spec in MODEL_CATALOG.items() if spec["provider"] == norm_p]


def validate_model(provider: str, model: Optional[str]) -> str:
    norm_p = normalize_provider_name(provider)
    if not model or not model.strip():
        return DEFAULT_MODELS[norm_p]

    clean_m = model.strip()
    if clean_m not in MODEL_CATALOG:
        supported = get_supported_models(norm_p)
        raise ValueError(
            f"Invalid model '{clean_m}' for provider '{norm_p}'. "
            f"Supported models are: {', '.join(supported)}. Silent model substitution is disabled."
        )

    spec = MODEL_CATALOG[clean_m]
    if spec["provider"] != norm_p:
        raise ValueError(
            f"Model '{clean_m}' belongs to provider '{spec['provider']}', not '{norm_p}'."
        )
    return clean_m


def get_model_spec(model_name: str) -> Optional[Dict[str, Any]]:
    return MODEL_CATALOG.get(model_name)
