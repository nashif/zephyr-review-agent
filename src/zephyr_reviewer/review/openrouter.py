# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""OpenRouter chat completions with tool calling, model fallback and retries.

Model selection is OpenRouter's job: the request carries the ordered ``models``
list and OpenRouter routes to the first that accepts it. ``provider`` passes
routing preferences straight through (ordering, data-collection policy,
required parameters), see https://openrouter.ai/docs/features/provider-routing.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .text import without_surrogates

RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenRouterError(RuntimeError):
    pass


class ContextLengthError(OpenRouterError):
    """The request exceeded the model's context window: a budget signal, not a transport failure."""


_CONTEXT_LENGTH = re.compile(
    r"context[ _]length|maximum context|context window|too many tokens|prompt is too long|input is too long"
    r"|exceeds? the (?:maximum|limit)|reduce the length|token limit",
    re.I,
)


def _is_context_length(status: int, text: str) -> bool:
    return status in (400, 413, 422) and bool(_CONTEXT_LENGTH.search(text or ""))


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    requests: int = 0
    cached_tokens: int = 0  # the part of prompt_tokens the provider read from its prompt cache

    def add(self, usage: dict[str, Any] | None) -> None:
        self.requests += 1
        if not usage:
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.total_tokens += int(usage.get("total_tokens") or 0)
        self.cost += float(usage.get("cost") or 0.0)
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            self.cached_tokens += int(details.get("cached_tokens") or 0)

    def merge(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cost += other.cost
        self.requests += other.requests
        self.cached_tokens += other.cached_tokens

    @property
    def cached_share(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": round(self.cost, 4),
            "requests": self.requests,
        }


@dataclass
class ChatResult:
    message: dict[str, Any]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return list(self.message.get("tool_calls") or [])

    @property
    def content(self) -> str:
        c = self.message.get("content")
        if isinstance(c, list):  # some providers return content parts
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return c or ""


class OpenRouter:
    def __init__(
        self,
        api_key: str,
        models: list[str],
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        temperature: float = 0.1,
        max_tokens: int = 8000,
        provider: dict[str, Any] | None = None,
        timeout: float = 300.0,
        max_attempts: int = 5,
        client: httpx.Client | None = None,
        on_event: Callable[[str], None] | None = None,
    ):
        if not models:
            raise ValueError("at least one model is required")
        self.models = models
        self.timeout = timeout
        self.on_event = on_event or (lambda msg: None)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.provider = provider
        self.max_attempts = max_attempts
        self.base_url = base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/nashif/zephyr-reviewer",
            "X-Title": "zephyr-reviewer",
        }
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        if client is not None:
            self._client.headers.update(headers)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.models[0],
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "usage": {"include": True},
        }
        if len(self.models) > 1:
            payload["models"] = self.models
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
            payload["parallel_tool_calls"] = True
        if self.provider:
            payload["provider"] = self.provider

        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                last_error = f"no response within {self.timeout:.0f}s ({type(exc).__name__})"
                if attempt < self.max_attempts:
                    self._retry(attempt, None, last_error)
                continue
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                if attempt < self.max_attempts:
                    self._retry(attempt, None, last_error)
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_attempts:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                self._retry(attempt, resp.headers.get("Retry-After"), last_error)
                continue
            if resp.status_code >= 400:
                if _is_context_length(resp.status_code, resp.text):
                    raise ContextLengthError(f"HTTP {resp.status_code}: {resp.text[:600]}")
                raise OpenRouterError(f"HTTP {resp.status_code}: {resp.text[:1000]}")
            data = resp.json()
            if "error" in data and not data.get("choices"):
                err = data["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                code = err.get("code") if isinstance(err, dict) else None
                if code in RETRY_STATUSES and attempt < self.max_attempts:
                    last_error = f"OpenRouter {code}: {msg[:300]}"
                    self._retry(attempt, None, last_error)
                    continue
                if _is_context_length(int(code or 400), msg):
                    raise ContextLengthError(f"OpenRouter error: {msg}")
                raise OpenRouterError(f"OpenRouter error: {msg}")
            choices = data.get("choices") or []
            if not choices:
                raise OpenRouterError(f"no choices in response: {json.dumps(data)[:500]}")
            choice = choices[0]
            return ChatResult(
                message=without_surrogates(choice.get("message") or {}),  # a model's escapes, made encodable
                model=data.get("model", payload["model"]),
                usage=data.get("usage") or {},
                finish_reason=choice.get("finish_reason") or "",
            )
        raise OpenRouterError(f"giving up after {self.max_attempts} attempts: {last_error}")

    def _retry(self, attempt: int, retry_after: str | None, why: str) -> None:
        delay = self._delay(attempt, retry_after)
        self.on_event(f"model request failed ({why}); retry {attempt + 1}/{self.max_attempts} in {delay:.0f}s")
        self._sleep(delay)

    @staticmethod
    def _delay(attempt: int, retry_after: str | None) -> float:
        delay = min(60.0, 2.0**attempt + random.uniform(0, 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        return delay

    @staticmethod
    def _sleep(delay: float) -> None:
        time.sleep(delay)


def assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields providers accept back; preserve reasoning blocks where OpenRouter returns them."""
    out: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    if message.get("reasoning_details"):
        out["reasoning_details"] = message["reasoning_details"]
    return out
