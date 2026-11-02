"""
JobHunt AI Studio - Core AI Engine
Provides run-scoped client isolation, centralized model validation,
Gemini project-level quota preservation, bounded retries with Retry-After support,
and immediate actionable handling of authentication errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI, AuthenticationError, RateLimitError

from core.models_catalog import (
    DEFAULT_MODELS,
    normalize_provider_name,
    validate_model,
)

logger = logging.getLogger("JobHuntAIStudio")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def extract_json_str(raw: str) -> str:
    """Robustly extract valid JSON substring from LLM response."""
    if not raw:
        return "{}"
    raw = raw.strip()
    m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
    if m:
        return m.group(1).strip()
    first_brace = raw.find('{')
    last_brace = raw.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return raw[first_brace:last_brace + 1].strip()
    return raw


class GeminiProjectLimiter:
    """
    Project-level rate limiter for Google Gemini API:
    - Enforces configured RPM across all requests for a specific key/project.
    - Preserves project-level quota (15 RPM free tier).
    - Honors full cooldown duration upon 429 / ResourceExhausted errors.
    """

    def __init__(self, rpm_limit: int = 15):
        self.rpm_limit = rpm_limit
        self._lock = asyncio.Lock()
        self._call_timestamps: List[float] = []
        self._cooldown_until: float = 0.0

    def get_wait_time(self) -> float:
        """Returns the seconds until this limiter can accept a call."""
        now = time.time()
        cooldown_wait = max(0.0, self._cooldown_until - now)
        valid_ts = [t for t in self._call_timestamps if now - t < 60.0]
        if len(valid_ts) >= self.rpm_limit:
            earliest = valid_ts[0]
            rpm_wait = max(0.0, 60.0 - (now - earliest) + 0.1)
        else:
            rpm_wait = 0.0
        return max(cooldown_wait, rpm_wait)

    async def acquire_slot(self):
        async with self._lock:
            now = time.time()
            # 1. Check active cooldown
            if self._cooldown_until > now:
                wait_sec = self._cooldown_until - now
                logger.info(f"⏳ Gemini project cooldown active. Waiting {wait_sec:.1f}s...")
                await asyncio.sleep(wait_sec)
                now = time.time()

            # 2. Sliding window RPM purge (60 seconds)
            self._call_timestamps = [t for t in self._call_timestamps if now - t < 60.0]

            # 3. If at RPM capacity, throttle
            if len(self._call_timestamps) >= self.rpm_limit:
                earliest = self._call_timestamps[0]
                sleep_needed = max(0.1, 60.0 - (now - earliest) + 0.1)
                logger.info(f"⏳ Gemini rate limiter throttling ({len(self._call_timestamps)} calls in last 60s): sleeping {sleep_needed:.2f}s...")
                await asyncio.sleep(sleep_needed)
                now = time.time()

            self._call_timestamps.append(time.time())

    def trigger_cooldown(self, seconds: float = 25.0):
        self._cooldown_until = max(self._cooldown_until, time.time() + seconds)
        logger.warning(f"⚠️ Gemini project-level rate limit hit. Enforcing {seconds:.1f}s full cooldown wait.")


class GeminiKeyPool:
    """
    Manages user-provided Gemini keys with independent per-key rate limiting,
    safe masking, and true multi-project throughput scaling.
    """

    def __init__(self, keys: Optional[List[str]] = None, rpm_limit: int = 15):
        self._keys: List[str] = [k.strip() for k in keys if k and k.strip()] if keys else []
        self._rpm_limit: int = rpm_limit
        self._limiters: Dict[str, GeminiProjectLimiter] = {}
        self._index: int = 0
        self.stats: Dict[str, Dict[str, int]] = {}
        self._setup_limiters_and_stats()

    def _setup_limiters_and_stats(self):
        for k in self._keys:
            if k not in self._limiters:
                self._limiters[k] = GeminiProjectLimiter(rpm_limit=self._rpm_limit)
            mask = self.get_mask(k)
            if mask not in self.stats:
                self.stats[mask] = {"calls": 0, "success": 0, "rate_limits": 0, "errors": 0}

    @property
    def limiter(self) -> GeminiProjectLimiter:
        """Backwards compatibility for tests and inspections."""
        if self._keys and self._keys[0] in self._limiters:
            return self._limiters[self._keys[0]]
        return GeminiProjectLimiter(rpm_limit=self._rpm_limit)

    def update_keys(self, keys: List[str], rpm_limit: int = 15):
        self._keys = [k.strip() for k in keys if k and k.strip()]
        self._rpm_limit = rpm_limit
        for k in self._keys:
            if k in self._limiters:
                self._limiters[k].rpm_limit = rpm_limit
        self._setup_limiters_and_stats()

    def get_keys(self) -> List[str]:
        return self._keys

    def get_mask(self, key: str) -> str:
        if not key:
            return "EMPTY"
        return f"{key[:6]}...{key[-4:]}" if len(key) > 10 else key

    async def acquire_client(self) -> Tuple[AsyncOpenAI, str, str]:
        if not self._keys:
            raise ValueError("No Gemini API keys configured. Please provide your Gemini API key.")

        # Find the key with the shortest wait time
        # If multiple keys have 0 wait time, advance in round-robin order
        n = len(self._keys)
        best_key = None
        min_wait = float("inf")

        for i in range(n):
            cand_key = self._keys[(self._index + i) % n]
            limiter = self._limiters[cand_key]
            wait = limiter.get_wait_time()
            if wait < min_wait:
                min_wait = wait
                best_key = cand_key
                if wait == 0.0:
                    self._index = (self._index + i + 1) % n
                    break

        if not best_key:
            best_key = self._keys[self._index % n]
            self._index = (self._index + 1) % n

        target_limiter = self._limiters[best_key]
        await target_limiter.acquire_slot()

        mask = self.get_mask(best_key)
        if mask in self.stats:
            self.stats[mask]["calls"] += 1

        client = AsyncOpenAI(
            api_key=best_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            timeout=45.0,
        )
        return client, best_key, mask

    def record_success(self, key: str):
        mask = self.get_mask(key)
        if mask in self.stats:
            self.stats[mask]["success"] += 1

    def record_rate_limit(self, key: str, cooldown_seconds: float = 25.0):
        mask = self.get_mask(key)
        if mask in self.stats:
            self.stats[mask]["rate_limits"] += 1
        if key in self._limiters:
            self._limiters[key].trigger_cooldown(cooldown_seconds)

    def record_error(self, key: str, error_msg: str):
        mask = self.get_mask(key)
        if mask in self.stats:
            self.stats[mask]["errors"] += 1


class ScopedAIClient:
    """
    Run-scoped AI client executing provider requests without mutating process-wide
    global variables or environment variables.
    """

    def __init__(
        self,
        provider: str,
        api_keys: List[str],
        model: Optional[str] = None,
        rpm_limit: int = 15,
        request_timeout: float = 45.0,
    ):
        self.provider = normalize_provider_name(provider)
        self.api_keys = [k.strip() for k in api_keys if k and k.strip()]
        self.model = validate_model(self.provider, model)
        self.request_timeout = request_timeout

        if not self.api_keys:
            raise ValueError(f"API key required for provider '{self.provider}'. None provided.")

        if self.provider == "gemini":
            self.gemini_pool = GeminiKeyPool(keys=self.api_keys, rpm_limit=rpm_limit)
        else:
            self.gemini_pool = None

    async def call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        thinking: bool = False,
    ) -> Tuple[str, int, int]:
        """
        Executes LLM completion and returns (response_json_string, prompt_tokens, completion_tokens).
        Validates outputs and never silently invents defaults.
        """
        if self.provider == "gemini":
            return await self._call_gemini(system_prompt, user_prompt, temperature)
        elif self.provider == "deepseek":
            return await self._call_deepseek(system_prompt, user_prompt, temperature, thinking)
        elif self.provider == "openai":
            return await self._call_openai(system_prompt, user_prompt, temperature)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

    async def _call_gemini(self, system_prompt: str, user_prompt: str, temperature: float) -> Tuple[str, int, int]:
        max_attempts = max(3, len(self.api_keys) * 2)

        for attempt in range(max_attempts):
            client = None
            active_key = None
            try:
                client, active_key, mask = await self.gemini_pool.acquire_client()
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    timeout=self.request_timeout,
                )
                self.gemini_pool.record_success(active_key)
                content = response.choices[0].message.content or "{}"
                prompt_tok = response.usage.prompt_tokens if response.usage else 0
                comp_tok = response.usage.completion_tokens if response.usage else 0
                return extract_json_str(content), prompt_tok, comp_tok

            except AuthenticationError as auth_err:
                # 401: Authentication / Invalid Key -> actionable error immediately
                logger.error(f"Gemini API key rejected (401 Unauthorized): {auth_err}")
                raise ValueError(f"Gemini authentication failed: Invalid API key ({auth_err}).")

            except Exception as e:
                err_str = str(e).lower()
                is_invalid_key = (
                    "please pass a valid api key" in err_str
                    or "api_key_invalid" in err_str
                    or "invalid api key" in err_str
                    or ("invalid_argument" in err_str and "key" in err_str)
                    or "invalid api_key" in err_str
                )
                if is_invalid_key:
                    logger.error(f"Gemini API key rejected as invalid: {e}")
                    raise ValueError(f"Gemini authentication failed: Invalid API key ({e}).")

                is_rate_limit = (
                    "429" in err_str
                    or "resource_exhausted" in err_str
                    or "quota" in err_str
                    or "rate" in err_str
                )

                if is_rate_limit and active_key:
                    cooldown = 25.0
                    # Check for Retry-After hints
                    m = re.search(r'retry-after:?\s*(\d+)', err_str)
                    if m:
                        cooldown = max(float(m.group(1)), 25.0)
                    self.gemini_pool.record_rate_limit(active_key, cooldown)
                elif active_key:
                    self.gemini_pool.record_error(active_key, str(e))

                if attempt == max_attempts - 1:
                    logger.error(f"Gemini API exhausted {max_attempts} attempts: {e}")
                    raise

                delay = 1.0 if is_rate_limit else (2.0 + random.uniform(0.5, 1.5))
                logger.warning(
                    f"Gemini request failed ({e}). Retrying in {delay:.1f}s (Attempt {attempt + 1}/{max_attempts})..."
                )
                await asyncio.sleep(delay)
            finally:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass

        raise RuntimeError("Gemini API call failed after max attempts.")

    async def _call_deepseek(self, system_prompt: str, user_prompt: str, temperature: float, thinking: bool) -> Tuple[str, int, int]:
        client = AsyncOpenAI(
            api_key=self.api_keys[0],
            base_url="https://api.deepseek.com",
            timeout=self.request_timeout,
        )
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
        if thinking and "reasoner" not in self.model.lower():
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        max_attempts = 3
        try:
            for attempt in range(max_attempts):
                try:
                    response = await client.chat.completions.create(**kwargs)
                    content = response.choices[0].message.content or "{}"
                    prompt_tok = response.usage.prompt_tokens if response.usage else 0
                    comp_tok = response.usage.completion_tokens if response.usage else 0
                    return extract_json_str(content), prompt_tok, comp_tok
                except AuthenticationError as auth_err:
                    logger.error(f"DeepSeek API key rejected (401 Unauthorized): {auth_err}")
                    raise ValueError(f"DeepSeek authentication failed: Invalid API key ({auth_err}).")
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    err_str = str(e).lower()
                    is_rate_limit = "429" in err_str or "rate" in err_str
                    delay = (2 ** attempt) * (4.0 if is_rate_limit else 2.0) + random.uniform(0.5, 1.0)
                    logger.warning(f"DeepSeek API retry in {delay:.1f}s ({e})...")
                    await asyncio.sleep(delay)
        finally:
            await client.close()

        raise RuntimeError("DeepSeek call failed.")

    async def _call_openai(self, system_prompt: str, user_prompt: str, temperature: float) -> Tuple[str, int, int]:
        client = AsyncOpenAI(
            api_key=self.api_keys[0],
            timeout=self.request_timeout,
        )
        max_attempts = 3
        try:
            for attempt in range(max_attempts):
                try:
                    response = await client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        temperature=temperature,
                        response_format={"type": "json_object"},
                        timeout=self.request_timeout,
                    )
                    content = response.choices[0].message.content or "{}"
                    prompt_tok = response.usage.prompt_tokens if response.usage else 0
                    comp_tok = response.usage.completion_tokens if response.usage else 0
                    return extract_json_str(content), prompt_tok, comp_tok
                except AuthenticationError as auth_err:
                    logger.error(f"OpenAI API key rejected (401 Unauthorized): {auth_err}")
                    raise ValueError(f"OpenAI authentication failed: Invalid API key ({auth_err}).")
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    delay = (2 ** attempt) * 2.0 + random.uniform(0.5, 1.0)
                    logger.warning(f"OpenAI API retry in {delay:.1f}s ({e})...")
                    await asyncio.sleep(delay)
        finally:
            await client.close()

        raise RuntimeError("OpenAI call failed.")


class AIEngine:
    """
    Backward-compatible adapter for existing call sites.
    Delegates to ScopedAIClient without polluting global process environment.
    """

    def __init__(self, default_provider: str = "gemini"):
        self.provider = default_provider
        self._keys: List[str] = []
        self._rpm: int = 15
        self.gemini_pool = GeminiKeyPool(keys=[], rpm_limit=15)

    def configure_keys(
        self,
        provider: str,
        gemini_keys: Optional[List[str]] = None,
        deepseek_key: Optional[str] = None,
        openai_key: Optional[str] = None,
        gemini_rpm: int = 15,
    ):
        self.provider = normalize_provider_name(provider)
        self._rpm = gemini_rpm
        if self.provider == "gemini" and gemini_keys:
            self._keys = [k.strip() for k in gemini_keys if k and k.strip()]
            self.gemini_pool.update_keys(self._keys, rpm_limit=gemini_rpm)
        elif self.provider == "deepseek" and deepseek_key:
            self._keys = [deepseek_key.strip()]
        elif self.provider == "openai" and openai_key:
            self._keys = [openai_key.strip()]

    async def call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        temperature: float = 0.3,
        thinking: bool = False,
        api_keys: Optional[List[str]] = None,
    ) -> str:
        active_provider = normalize_provider_name(provider or self.provider)
        keys_to_use = [k.strip() for k in api_keys if k and k.strip()] if api_keys else self._keys

        if not keys_to_use:
            raise ValueError(f"No API keys supplied for {active_provider}.")

        target_model = model or DEFAULT_MODELS[active_provider]
        target_model = validate_model(active_provider, target_model)

        scoped_client = ScopedAIClient(
            provider=active_provider,
            api_keys=keys_to_use,
            model=target_model,
            rpm_limit=self._rpm,
        )
        content_json, _, _ = await scoped_client.call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            thinking=thinking,
        )
        return content_json


ai_engine_instance = AIEngine()
