"""Thin OpenAI-compatible LLM client.

WorkGuard never lets the LLM return free-form text into the pipeline: every
call asks for JSON and the caller validates against a Pydantic/手动 schema.
When no API key is configured the agents fall back to deterministic heuristic
extraction so the entire demo runs offline.

Every call records purpose/model/tokens/latency into the UsageTracker, so
token cost is always measured, never guessed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from backend.config import settings
from backend.llm.usage import get_usage

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self) -> None:
        self._client: Any = None
        if settings.llm_enabled():
            try:
                from openai import OpenAI

                kwargs: dict[str, Any] = {"api_key": settings.openai_api_key}
                if settings.openai_base_url:
                    kwargs["base_url"] = settings.openai_base_url
                # explicit timeout + single retry: a hung provider must never
                # stall ingestion (the heuristic fallback takes over instead)
                kwargs["timeout"] = float(os.getenv("WORKGUARD_LLM_TIMEOUT", "60"))
                kwargs["max_retries"] = 1
                self._client = OpenAI(**kwargs)
            except Exception as exc:  # pragma: no cover
                logger.warning("LLM init failed, falling back to heuristics: %s", exc)
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def _call(self, messages: list[dict[str, str]], json_mode: bool):
        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": 0,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return self._client.chat.completions.create(**kwargs)

    @staticmethod
    def _strip_fence(content: str) -> str:
        return re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()

    def complete_json(self, system: str, user: str, purpose: str = "unspecified") -> dict | list | None:
        """Ask the model for a JSON object. Returns None on any failure.

        Token usage and latency of every attempt are recorded in the
        UsageTracker regardless of success."""
        if not self._client:
            return None
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        usage = get_usage()

        def _record(started: float, ok: bool, resp=None, error: str = "") -> None:
            latency = (time.perf_counter() - started) * 1000
            prompt = completion = 0
            if resp is not None and getattr(resp, "usage", None):
                prompt = resp.usage.prompt_tokens or 0
                completion = resp.usage.completion_tokens or 0
            usage.record(purpose, settings.llm_model, prompt, completion, latency, ok, error)

        resp = None
        try:
            started = time.perf_counter()
            resp = self._call(messages, json_mode=True)
            data = json.loads(resp.choices[0].message.content or "")
            _record(started, True, resp)
            return data
        except Exception as first_error:
            _record(started, False, locals().get("resp"), str(first_error))
            # retry once without response_format (some compatible endpoints ignore it)
            try:
                started = time.perf_counter()
                resp = None
                resp = self._call(messages, json_mode=False)
                content = self._strip_fence(resp.choices[0].message.content or "")
                data = json.loads(content)
                _record(started, True, resp)
                return data
            except Exception as exc:
                logger.warning("LLM call failed (%s): %s", purpose, exc)
                _record(started, False, resp, str(exc) or str(first_error))
                return None

    def metrics(self) -> dict:
        """Return the process-scoped usage window used by evaluation scripts."""
        return get_usage().summary()


_singleton: LLMClient | None = None


def get_llm() -> LLMClient:
    global _singleton
    if _singleton is None:
        _singleton = LLMClient()
    return _singleton
