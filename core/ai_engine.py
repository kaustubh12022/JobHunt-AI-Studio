"""
JobHunt AI Studio - DeepSeek Engine
One provider, one model, one owner.

Wraps the DeepSeek Chat Completions API (OpenAI-compatible) with:
- JSON-object responses and tolerant JSON extraction
- A bounded in-flight request semaphore
- Actionable authentication/balance errors
- Bounded exponential retries with Retry-After support
- Optional multimodal payloads (the reviewer inspects a rendered PDF image)
- Optional high-effort thinking for drafting and review
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from openai import AsyncOpenAI

from core.ai_errors import CredentialsRequired
from core.models_catalog import validate_model
from core.settings import (
    API_BASE_URL,
    CONCURRENCY,
    MODEL,
    REQUEST_TIMEOUT,
    deepseek_api_key,
)

logger = logging.getLogger("JobHuntAIStudio")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PromptContent = Union[str, List[Dict[str, Any]]]


def extract_json_str(raw: str) -> str:
    """Robustly extract the JSON object from an LLM response."""
    if not raw:
        return "{}"
    raw = raw.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if match:
        return match.group(1).strip()
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return raw[first_brace:last_brace + 1].strip()
    return raw


def retry_after(error: Exception) -> Optional[float]:
    """Read a Retry-After header or provider retryDelay hint, when present."""
    headers = getattr(getattr(error, "response", None), "headers", {}) or {}
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    if value:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                return max(0.0, parsedate_to_datetime(str(value)).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        payload = body.get("error", body)
        details = payload.get("details") if isinstance(payload, dict) else None
        for detail in details or []:
            delay = detail.get("retryDelay") if isinstance(detail, dict) else None
            if isinstance(delay, str) and re.fullmatch(r"\d+(?:\.\d+)?s", delay):
                return float(delay[:-1])
    return None


class DeepSeekClient:
    """Run-scoped DeepSeek client. Credentials stay in memory only."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        *,
        concurrency: int = CONCURRENCY,
        request_timeout: float = REQUEST_TIMEOUT,
        max_attempts: int = 4,
    ):
        key = (api_key if api_key is not None else deepseek_api_key()).strip()
        if not key:
            raise CredentialsRequired(
                "The DeepSeek API key is not configured on this server. "
                "Set DEEPSEEK_API_KEY and restart the studio."
            )
        self.api_key = key
        self.model = validate_model("deepseek", model or MODEL)
        self.request_timeout = request_timeout
        self.max_attempts = max(1, max_attempts)
        self._client = AsyncOpenAI(
            api_key=key,
            base_url=API_BASE_URL,
            timeout=request_timeout,
            max_retries=0,
        )
        self._slots = asyncio.Semaphore(max(1, concurrency))

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:  # pragma: no cover - closing must never mask a result
            pass

    async def __aenter__(self) -> "DeepSeekClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    def _request_kwargs(
        self,
        system_prompt: str,
        user_prompt: PromptContent,
        temperature: float,
        thinking: bool,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "timeout": self.request_timeout,
        }
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = "high"
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return kwargs

    async def call_llm(
        self,
        system_prompt: str,
        user_prompt: PromptContent,
        temperature: float = 0.3,
        thinking: bool = False,
    ) -> Tuple[str, int, int]:
        """Return (json_response, prompt_tokens, completion_tokens)."""
        kwargs = self._request_kwargs(system_prompt, user_prompt, temperature, thinking)
        last_error: Optional[Exception] = None

        for attempt in range(self.max_attempts):
            try:
                async with self._slots:
                    response = await self._client.chat.completions.create(**kwargs)
                content = (response.choices[0].message.content or "{}") if response.choices else "{}"
                usage = response.usage
                prompt_tokens = int(usage.prompt_tokens) if usage else 0
                completion_tokens = int(usage.completion_tokens) if usage else 0
                return extract_json_str(content), prompt_tokens, completion_tokens
            except Exception as error:  # noqa: BLE001 - translated into domain errors below
                status = getattr(error, "status_code", None)
                last_error = error
                if status in (401, 402, 403):
                    raise CredentialsRequired(
                        "DeepSeek rejected the API key or the account has no remaining balance. "
                        "Update DEEPSEEK_API_KEY and resume the batch."
                    ) from None
                if status is not None and status not in (408, 409, 429) and status < 500:
                    raise RuntimeError(
                        f"DeepSeek rejected the request (HTTP {status}). "
                        "Check the locked model and the input size."
                    ) from None

                if attempt == self.max_attempts - 1:
                    break
                delay = retry_after(error)
                if delay is None:
                    delay = min(30.0, float(2 ** attempt)) + random.uniform(0.0, 0.5)
                logger.warning(
                    "DeepSeek call failed (attempt %s/%s); retrying in %.1fs: %s",
                    attempt + 1, self.max_attempts, delay, error,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(
            "DeepSeek is temporarily unavailable. Resume the batch to retry from the last checkpoint."
        ) from last_error


_default_client: Optional[DeepSeekClient] = None


def default_client() -> DeepSeekClient:
    """Lazily created process-wide client used by headless callers."""
    global _default_client
    if _default_client is None:
        _default_client = DeepSeekClient()
    return _default_client
