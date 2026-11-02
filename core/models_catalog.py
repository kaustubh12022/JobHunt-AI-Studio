"""
JobHunt AI Studio - Centralized Model Catalog & Pricing
Provides canonical model definitions, supported options per provider,
and dated pricing reference. Forbids silent model substitution.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


SUPPORTED_PROVIDERS = ("gemini", "deepseek", "openai")

DEFAULT_MODELS: Dict[str, str] = {
    "gemini": "gemini-3.5-flash-lite",
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
}

# Pricing per million tokens (dated as of late 2024 / 2025 upstream pricing)
# Gemini 3.5 Flash-Lite: $0.075 / 1M input, $0.30 / 1M output (Free tier: $0.00)
# DeepSeek Chat (DeepSeek-V3 / Flash endpoint): $0.27 / 1M input (cache miss), $1.10 / 1M output
# OpenAI GPT-4o-mini: $0.15 / 1M input, $0.60 / 1M output
MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    "gemini-3.5-flash-lite": {
        "provider": "gemini",
        "display_name": "Gemini 3.5 Flash-Lite",
        "input_per_m": 0.075,
        "output_per_m": 0.30,
        "context_window": 1_048_576,
        "is_default": True,
    },
    "gemini-2.5-flash": {
        "provider": "gemini",
        "display_name": "Gemini 2.5 Flash",
        "input_per_m": 0.075,
        "output_per_m": 0.30,
        "context_window": 1_048_576,
        "is_default": False,
    },
    "gemini-2.5-pro": {
        "provider": "gemini",
        "display_name": "Gemini 2.5 Pro",
        "input_per_m": 1.25,
        "output_per_m": 5.00,
        "context_window": 2_097_152,
        "is_default": False,
    },
    "gemini-1.5-flash": {
        "provider": "gemini",
        "display_name": "Gemini 1.5 Flash",
        "input_per_m": 0.075,
        "output_per_m": 0.30,
        "context_window": 1_048_576,
        "is_default": False,
    },
    "deepseek-chat": {
        "provider": "deepseek",
        "display_name": "DeepSeek Chat (V3 / Flash)",
        "input_per_m": 0.27,
        "output_per_m": 1.10,
        "context_window": 65_536,
        "is_default": True,
    },
    "deepseek-reasoner": {
        "provider": "deepseek",
        "display_name": "DeepSeek Reasoner (R1)",
        "input_per_m": 0.55,
        "output_per_m": 2.19,
        "context_window": 65_536,
        "is_default": False,
    },
    "gpt-4o-mini": {
        "provider": "openai",
        "display_name": "OpenAI GPT-4o-mini",
        "input_per_m": 0.15,
        "output_per_m": 0.60,
        "context_window": 128_000,
        "is_default": True,
    },
    "gpt-4o": {
        "provider": "openai",
        "display_name": "OpenAI GPT-4o",
        "input_per_m": 2.50,
        "output_per_m": 10.00,
        "context_window": 128_000,
        "is_default": False,
    },
}


def normalize_provider_name(provider_str: str) -> str:
    p = (provider_str or "").strip().lower()
    if "gemini" in p or "google" in p:
        return "gemini"
    if "deepseek" in p:
        return "deepseek"
    if "openai" in p:
        return "openai"
    raise ValueError(f"Unsupported AI provider '{provider_str}'. Allowed providers: {SUPPORTED_PROVIDERS}")


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
