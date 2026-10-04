"""Estimation théorique des appels API, uniquement à partir de l'usage connu."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from .config import setting
from .models import ApiCost

logger = logging.getLogger(__name__)


@dataclass
class UsageCounter:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    complete: bool = True


_usage: ContextVar[UsageCounter | None] = ContextVar("api_usage", default=None)


@contextmanager
def capture_usage(counter: UsageCounter) -> Iterator[None]:
    token = _usage.set(counter)
    try:
        yield
    finally:
        _usage.reset(token)


def record_llm_usage(usage: dict | None) -> None:
    counter = _usage.get()
    if counter is None:
        return
    counter.llm_calls += 1
    usage = usage or {}
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int) and prompt >= 0 and completion >= 0:
        counter.input_tokens += prompt
        counter.output_tokens += completion
    else:
        counter.complete = False


def estimate(config: dict, counter: UsageCounter, searches: int,
             credits: int | None) -> ApiCost:
    model = os.environ.get("SIGNAL_MATIN_LLM_MODEL", "").strip()
    rates = (setting(config, "api_cost.models", {}) or {}).get(model)
    llm = None
    if counter.complete and isinstance(rates, dict):
        llm = round((counter.input_tokens * float(rates["input_per_million_usd"]) +
                     counter.output_tokens * float(rates["output_per_million_usd"])) / 1_000_000, 6)
    per_credit = setting(config, "api_cost.tavily_usd_per_credit", None)
    tavily = round(credits * float(per_credit), 6) if credits is not None and per_credit is not None else None
    total = round(llm + tavily, 6) if llm is not None and tavily is not None else None
    result = ApiCost(model=model, llm_calls=counter.llm_calls,
                     input_tokens=counter.input_tokens if counter.complete else None,
                     output_tokens=counter.output_tokens if counter.complete else None,
                     tavily_searches=searches, llm_usd=llm, tavily_usd=tavily, total_usd=total)
    logger.info("[Cost] LLM calls: %d | input tokens: %s | output tokens: %s | "
                "Tavily searches: %d | estimated cost: %s",
                result.llm_calls, result.input_tokens, result.output_tokens,
                searches, f"${total:.4f}" if total is not None else "estimation indisponible")
    return result
