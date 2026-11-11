# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Bridge between the review agent and the source-intelligence MCP server.

Spawns the server over stdio (one per review, torn down with the review),
or connects in-process for tests, or to a URL once a shared service exists.
Exposes the server's tools in the OpenAI function-calling shape OpenRouter
expects, and turns tool results back into bounded text.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer

from .text import without_surrogates


def stdio_params(
    repo_root: str, head: str, base: str, max_chars: int, python: str = sys.executable
) -> StdioServerParameters:
    return StdioServerParameters(
        command=python,
        args=[
            "-m",
            "zephyr_reviewer.mcp_server",
            "--repo",
            repo_root,
            "--head",
            head,
            "--base",
            base,
            "--max-chars",
            str(max_chars),
        ],
    )


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result_chars: int
    is_error: bool
    seconds: float


@dataclass
class ToolBridge:
    target: StdioServerParameters | MCPServer | str
    max_result_chars: int = 24_000
    call_timeout: float = 120.0  # seconds per tool call; a hung lookup becomes an error the model can see
    tools: list[dict[str, Any]] = field(default_factory=list)
    calls: list[ToolCallRecord] = field(default_factory=list)
    _client: Client | None = None

    async def __aenter__(self) -> ToolBridge:
        self._client = Client(self.target)
        await self._client.__aenter__()
        listed = await self._client.list_tools()
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "parameters": _clean_schema(t.input_schema),
                },
            }
            for t in listed.tools
        ]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    @property
    def tool_names(self) -> set[str]:
        return {t["function"]["name"] for t in self.tools}

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        text, _ = await self.call_with_record(name, arguments)
        return text

    def _timeout_text(self, name: str) -> str:
        return _timeout_message(name, self.call_timeout)

    async def call_with_record(self, name: str, arguments: dict[str, Any]) -> tuple[str, ToolCallRecord]:
        assert self._client is not None, "bridge not started"
        started = time.monotonic()
        arguments = without_surrogates(arguments)  # a lone surrogate in an argument would kill the transport
        try:
            result = await asyncio.wait_for(
                self._client.call_tool(name, arguments, read_timeout_seconds=self.call_timeout),
                timeout=self.call_timeout + 5,
            )
            texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
            text = "\n".join(texts) if texts else (str(result.structured_content or "") or "(empty result)")
            is_error = bool(result.is_error)
        except TimeoutError:
            text, is_error = self._timeout_text(name), True
        except Exception as exc:  # noqa: BLE001 - a failed lookup is information for the model, not a crash
            text, is_error = f"{type(exc).__name__}: {exc}", True
            if "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower():
                text = self._timeout_text(name)
        if is_error and not text.startswith("ERROR"):
            text = "ERROR: " + text
        if len(text) > self.max_result_chars:
            text = text[: self.max_result_chars] + f"\n... [result truncated to {self.max_result_chars} characters]"
        record = ToolCallRecord(name, arguments, len(text), is_error, time.monotonic() - started)
        self.calls.append(record)
        return text, record


def _timeout_message(name: str, seconds: float) -> str:
    return f"tool call {name} did not finish within {seconds:.0f}s; narrow the query or try another tool"


def _clean_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Strip pydantic artifacts (titles, defaults as anyOf-null) that some providers reject."""
    if not schema:
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out.pop("title", None)
    props = {}
    for key, prop in (schema.get("properties") or {}).items():
        p = dict(prop)
        p.pop("title", None)
        if "anyOf" in p:
            non_null = [a for a in p["anyOf"] if a.get("type") != "null"]
            if len(non_null) == 1:
                p.update(non_null[0])
                p.pop("anyOf", None)
        props[key] = p
    out["properties"] = props
    out.setdefault("type", "object")
    return out
