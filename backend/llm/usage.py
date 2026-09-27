"""Durable LLM token, latency and cost accounting.

Every provider attempt is persisted in the application database. ``reset``
starts a process-local measurement window for evaluators; it never deletes the
historical records that power the API after a restart.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select

from backend.config import settings
from backend.db import SessionLocal
from backend.models import LLMUsageRecord, uid, utcnow

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output) — estimates, overridable for evaluations.
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "deepseek-chat": (0.27, 1.10),
    "qwen-plus": (0.40, 1.20),
    "qwen-turbo": (0.05, 0.20),
    "qwen-max": (1.60, 6.40),
}
_FALLBACK_PRICE = (0.50, 1.50)


def price_for(model: str) -> tuple[float, float]:
    override_in = os.getenv("WORKGUARD_PRICE_IN_PER_1M")
    override_out = os.getenv("WORKGUARD_PRICE_OUT_PER_1M")
    if override_in and override_out:
        return float(override_in), float(override_out)
    return _PRICE_TABLE.get(model, _FALLBACK_PRICE)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = price_for(model)
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000


def _serialize(row: LLMUsageRecord) -> dict[str, Any]:
    return {
        "purpose": row.purpose,
        "model": row.model,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "latency_ms": row.latency_ms,
        "ok": row.ok,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class UsageTracker:
    """Thread-safe writer with database-backed cross-process aggregation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window_started_at: datetime | None = None
        self._fallback_records: list[dict[str, Any]] = []

    def record(
        self,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        ok: bool,
        error: str = "",
    ) -> None:
        values = {
            "purpose": purpose,
            "model": model,
            "prompt_tokens": max(int(prompt_tokens), 0),
            "completion_tokens": max(int(completion_tokens), 0),
            "latency_ms": round(max(float(latency_ms), 0.0), 1),
            "ok": bool(ok),
            "error": str(error)[:2000],
        }
        try:
            with SessionLocal() as session:
                session.add(LLMUsageRecord(id=uid("llmu"), **values))
                session.commit()
        except Exception as exc:  # accounting must never break the agent path
            logger.warning("could not persist LLM usage: %s", exc)
            with self._lock:
                self._fallback_records.append({**values, "created_at": utcnow().isoformat()})

    def reset(self) -> None:
        """Start a fresh reporting window without erasing durable history."""
        with self._lock:
            self._window_started_at = utcnow()
            self._fallback_records.clear()

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            window = self._window_started_at
            fallback = list(self._fallback_records)
        try:
            with SessionLocal() as session:
                query = select(LLMUsageRecord).order_by(LLMUsageRecord.created_at.asc())
                if window is not None:
                    query = query.where(LLMUsageRecord.created_at >= window)
                rows = session.scalars(query).all()
                return [_serialize(row) for row in rows] + fallback
        except Exception as exc:
            logger.warning("could not read persisted LLM usage: %s", exc)
            return fallback

    def summary(self) -> dict[str, Any]:
        records = self.records
        by_purpose: dict[str, dict[str, int | float]] = defaultdict(
            lambda: {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_ms": 0.0,
            }
        )
        total_prompt = total_completion = errors = 0
        total_latency = total_cost = 0.0
        models: set[str] = set()
        for record in records:
            purpose = str(record["purpose"])
            model = str(record["model"])
            prompt_tokens = int(record["prompt_tokens"])
            completion_tokens = int(record["completion_tokens"])
            latency_ms = float(record["latency_ms"])
            bucket = by_purpose[purpose]
            bucket["calls"] += 1
            bucket["prompt_tokens"] += prompt_tokens
            bucket["completion_tokens"] += completion_tokens
            bucket["latency_ms"] += latency_ms
            total_prompt += prompt_tokens
            total_completion += completion_tokens
            total_latency += latency_ms
            total_cost += estimate_cost(model, prompt_tokens, completion_tokens)
            errors += 0 if record["ok"] else 1
            models.add(model)
        calls = len(records)
        if len(models) == 1:
            model_name = next(iter(models))
        elif models:
            model_name = "multiple"
        else:
            model_name = settings.llm_model
        return {
            "model": model_name,
            "calls": calls,
            "errors": errors,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "estimated_cost_usd": round(total_cost, 6),
            "avg_latency_ms": round(total_latency / calls, 1) if calls else 0.0,
            "by_purpose": dict(by_purpose),
            "records": records,
        }


_singleton = UsageTracker()


def get_usage() -> UsageTracker:
    return _singleton
