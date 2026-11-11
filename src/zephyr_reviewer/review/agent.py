# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""The review loop: model proposes tool calls, the bridge answers, until submit_review."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .mcp_client import ToolBridge
from .openrouter import ContextLengthError, OpenRouter, OpenRouterError, Usage, assistant_message_for_history
from .prompts import budget_exhausted, nudge, rejection_message
from .schema import SUBMIT_REVIEW_TOOL, SUBMIT_TOOL_NAME, Review, parse_review
from .text import without_surrogates


def tool_arguments(raw: str | dict[str, Any]) -> dict[str, Any]:
    """A tool call's arguments as the model sent them, parsed and made encodable: a \\ud83d escape in the
    JSON becomes a lone surrogate on parsing, which no transport, file or request could carry."""
    return without_surrogates(json.loads(raw) if isinstance(raw, str) else dict(raw))


@dataclass
class LocalTool:
    """A tool answered by the orchestrator itself rather than the MCP server (e.g. PR discussion)."""

    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]


@dataclass
class TerminalTool:
    """The tool whose successful call ends an agent run: its schema, a parser returning (output, errors),
    and a one-line description of the output for the log."""

    schema: dict[str, Any]
    parse: Callable[[dict[str, Any]], tuple[Any, list[str]]]
    describe: Callable[[Any], str] = lambda out: "submitted"

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]


@dataclass
class AgentResult:
    output: Any
    steps: int
    usage: Usage
    models_used: list[str]
    forced: bool
    errors: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    tool_results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tool_call_count(self) -> int:
        return sum(1 for t in self.transcript if t.get("kind") == "tool_call" and not t.get("terminal"))


@dataclass
class RunResult:
    review: Review | None
    steps: int
    usage: Usage
    models_used: list[str]
    forced: bool
    errors: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    tool_results: list[dict[str, Any]] = field(default_factory=list)  # name, arguments, content (for a later stage)
    stages: list[dict[str, Any]] = field(default_factory=list)  # per-stage summaries when the review ran in stages
    review_map: dict[str, Any] | None = None  # concern lineage when the review ran as a specialist pipeline

    @property
    def tool_call_count(self) -> int:
        return sum(
            1
            for t in self.transcript
            if t.get("kind") == "tool_call" and t.get("name") != SUBMIT_TOOL_NAME and not t.get("terminal")
        )

    def stage_summary(self, name: str) -> dict[str, Any]:
        return {
            "stage": name,
            "models": list(self.models_used),
            "steps": self.steps,
            "tool_calls": self.tool_call_count,
            "forced": self.forced,
            "submitted": self.review is not None,
            "seconds": round(self.seconds, 1),
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
                "cost": round(self.usage.cost, 4),
                "requests": self.usage.requests,
            },
        }


async def run_agent(
    llm: OpenRouter,
    bridge: ToolBridge | None,
    system: str,
    opening: str,
    terminal: TerminalTool,
    *,
    max_steps: int = 40,
    max_context_tokens: int = 160_000,
    max_rejections: int = 2,
    max_seconds: float = 0,
    extra_tools: list[LocalTool] | None = None,
    log=print,
    label: str = "",
    quiet: bool = False,
) -> AgentResult:
    """Model proposes tool calls, the bridge and local tools answer, until the terminal tool is accepted.

    ``max_seconds`` (0 = none) is a wall-clock budget: once exceeded, the next model call is forced to submit.
    While a model call is outstanding a heartbeat line is logged every ``HEARTBEAT_SECONDS``.
    ``quiet`` drops the per-step lines (model calls, tool calls, heartbeats) and logs one line when the
    agent finishes: what it submitted, or why it did not, with the step count and time."""
    started = time.monotonic()
    tag = f"[{label}] " if label else ""
    detail = (lambda msg: None) if quiet else log  # per-step chatter; errors still reach ``log`` via done()
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": opening}]
    local = {t.name: t for t in extra_tools or []}
    bridge_tools = list(bridge.tools) if bridge is not None else []
    bridge_names = bridge.tool_names if bridge is not None else set()
    tools = [*bridge_tools, *(t.schema for t in local.values()), terminal.schema]
    usage = Usage()
    models: list[str] = []
    transcript: list[dict[str, Any]] = []
    forced = False
    nudges = 0
    rejections = 0
    last_prompt_tokens = 0
    errors: list[str] = []
    tool_results: list[dict[str, Any]] = []
    compactions = 0
    forced_misses = 0

    def done(output: Any, step: int) -> AgentResult:
        seconds = time.monotonic() - started
        if quiet:
            what = terminal.describe(output) if output is not None else f"did not submit: {'; '.join(errors)}"
            log(f"{tag}{what} ({step} step(s), {seconds:.0f}s{', forced' if forced else ''})")
        return AgentResult(output, step, usage, models, forced, errors, transcript, seconds, tool_results)

    async def lookup(step: int, name: str, args: dict[str, Any]) -> str:
        """Answer one non-terminal tool call: a local tool or a bridge lookup, recorded in the transcript."""
        if name in local:
            t0 = time.monotonic()
            try:
                content = await local[name].handler(args)
            except Exception as exc:  # noqa: BLE001 - report to the model, keep reviewing
                content = f"ERROR: {type(exc).__name__}: {exc}"
            took = time.monotonic() - t0
            detail(f"{tag}[step {step}] {name}({_short(args)}) -> {len(content)} chars in {took:.1f}s")
            transcript.append(
                {
                    "kind": "tool_call",
                    "step": step,
                    "name": name,
                    "arguments": args,
                    "chars": len(content),
                    "seconds": took,
                }
            )
            tool_results.append({"name": name, "arguments": args, "content": content})
            return content
        assert bridge is not None
        label_call = f"{tag}[step {step}] still waiting for tool {name}({_short(args)})"
        content, rec = await _with_heartbeat(
            bridge.call_with_record(name, args),
            lambda waited, msg=label_call: detail(f"{msg} ({waited:.0f}s)"),
        )
        detail(
            f"{tag}[step {step}] {name}({_short(args)}) -> {rec.result_chars} chars"
            f"{' ERROR' if rec.is_error else ''} in {rec.seconds:.1f}s"
        )
        transcript.append(
            {
                "kind": "tool_call",
                "step": step,
                "name": name,
                "arguments": args,
                "chars": rec.result_chars,
                "error": rec.is_error,
                "seconds": rec.seconds,
            }
        )
        if not rec.is_error:
            tool_results.append({"name": name, "arguments": args, "content": content})
        return content

    # Once the budget is out the model is forced to submit; GRACE_STEPS more turns allow a rejected or
    # malformed forced submission to be retried instead of losing the whole run's work.
    for step in range(1, max_steps + GRACE_STEPS + 1):
        elapsed = time.monotonic() - started
        out_of_time = max_seconds > 0 and elapsed > max_seconds
        budget_out = step >= max_steps or last_prompt_tokens > max_context_tokens or out_of_time
        tool_choice: str | dict[str, Any] = "auto"
        if budget_out and not forced:
            forced = True
            why = f"time budget of {max_seconds:.0f}s" if out_of_time else "step or context budget"
            detail(f"{tag}[step {step}] {why} exhausted; forcing {terminal.name}")
            messages.append({"role": "user", "content": budget_exhausted(terminal.name)})
        if forced:
            # Some providers answer a named tool_choice with an empty message; after two such misses fall
            # back to "required" (any tool), which the loop then steers to the terminal tool by rejection.
            tool_choice = {"type": "function", "function": {"name": terminal.name}} if forced_misses < 2 else "required"

        approx_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 4
        detail(f"{tag}[step {step}] waiting for {llm.models[0]} ({len(messages)} messages, ~{approx_tokens:,} tokens)")
        t_call = time.monotonic()
        try:
            result = await _chat_with_heartbeat(llm, messages, tools, tool_choice, detail, tag, step)
        except ContextLengthError as exc:
            compactions += 1
            # First elide older tool results and retry; then everything and force submission; then give up.
            elided = compact_history(messages, keep_recent=4) if compactions == 1 else 0
            if elided:
                transcript.append({"kind": "compaction", "step": step, "elided": elided, "error": str(exc)[:300]})
                detail(f"{tag}[step {step}] context window exceeded; elided {elided} older tool result(s) and retrying")
                continue
            elided = compact_history(messages, keep_recent=0)
            if elided:
                transcript.append({"kind": "compaction", "step": step, "elided": elided, "error": str(exc)[:300]})
                forced = True
                detail(f"{tag}[step {step}] context still too large; elided all tool results, forcing submission")
                continue
            errors.append(f"context window exceeded and nothing left to compact: {str(exc)[:300]}")
            detail(f"{tag}[step {step}] {errors[-1]}")
            return done(None, step)
        except OpenRouterError as exc:
            errors.append(f"model request failed: {str(exc)[:500]}")
            detail(f"{tag}[step {step}] {errors[-1]}")
            return done(None, step)
        usage.add(result.usage)
        last_prompt_tokens = int((result.usage or {}).get("prompt_tokens") or 0)
        if result.model and result.model not in models:
            models.append(result.model)
        detail(
            f"{tag}[step {step}] {result.model or llm.models[0]} answered in {time.monotonic() - t_call:.1f}s "
            f"({len(result.tool_calls)} tool call(s), {last_prompt_tokens:,} prompt tokens)"
        )
        messages.append(assistant_message_for_history(result.message))
        transcript.append(
            {
                "kind": "assistant",
                "step": step,
                "model": result.model,
                "content": result.content[:4000],
                "tool_calls": [tc.get("function", {}).get("name") for tc in result.tool_calls],
                "usage": result.usage,
            }
        )

        if not result.tool_calls:
            nudges += 1
            if forced:
                forced_misses += 1
            despite = " despite forced tool_choice" if forced else ""
            detail(f"{tag}[step {step}] no tool call{despite}; nudging ({nudges})")
            if nudges > 2:
                forced = True
            messages.append(
                {"role": "user", "content": nudge(terminal.name) if not forced else budget_exhausted(terminal.name)}
            )
            continue

        # Parse every call first so a submission is judged before any lookups run; then run the lookups
        # concurrently (models often ask for several files at once) and answer them in the model's order.
        parsed: list[tuple[dict[str, Any], str, dict[str, Any] | None, str]] = []
        for tc in result.tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = tool_arguments(raw_args)
                content = ""
            except json.JSONDecodeError as exc:
                args = None
                content = f"ERROR: arguments were not valid JSON ({exc}). Call the tool again with a JSON object."
            parsed.append((tc, name, args, content))

        answers: dict[int, str] = {}
        for i, (_tc, name, args, content) in enumerate(parsed):
            if args is None:
                answers[i] = content
            elif name == terminal.name:
                output, errs = terminal.parse(args)
                transcript.append(
                    {"kind": "tool_call", "step": step, "name": name, "terminal": True, "ok": not errs, "errors": errs}
                )
                if output is not None and not errs:
                    detail(f"{tag}[step {step}] {terminal.describe(output)}")
                    return done(output, step)
                rejections += 1
                detail(f"{tag}[step {step}] submission rejected: {errs}")
                if rejections > max_rejections:
                    errors.extend(errs)
                    return done(None, step)
                answers[i] = rejection_message(errs, terminal.name)
            elif name not in local and not (bridge is not None and name in bridge_names):
                available = ", ".join(sorted(bridge_names | set(local)))
                answers[i] = f"ERROR: unknown tool {name!r}. Available: {available}, {terminal.name}."

        pending = [i for i in range(len(parsed)) if i not in answers]
        results = await asyncio.gather(*(lookup(step, parsed[i][1], parsed[i][2] or {}) for i in pending))
        answers.update(zip(pending, results, strict=True))
        for i, (tc, name, _args, _content) in enumerate(parsed):
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "name": name, "content": answers[i]})

    errors.append(f"model did not call {terminal.name} within {max_steps} steps (+{GRACE_STEPS} forced)")
    return done(None, max_steps + GRACE_STEPS)


HEARTBEAT_SECONDS = 30.0
GRACE_STEPS = 3  # extra turns after the step budget, all forced to the terminal tool


async def _with_heartbeat(awaitable, on_wait):
    """Await something, calling ``on_wait(seconds)`` every HEARTBEAT_SECONDS while it is outstanding."""
    task = asyncio.ensure_future(awaitable)
    waited = 0.0
    while True:
        done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
        if done:
            return task.result()
        waited += HEARTBEAT_SECONDS
        on_wait(waited)


async def _chat_with_heartbeat(llm, messages, tools, tool_choice, log, tag: str, step: int):
    """Run the blocking model call in a thread; say so periodically while it is outstanding."""
    return await _with_heartbeat(
        asyncio.to_thread(llm.chat, messages, tools, tool_choice),
        lambda waited: log(
            f"{tag}[step {step}] still waiting for {llm.models[0]} ({waited:.0f}s; request timeout {llm.timeout:.0f}s)"
        ),
    )


ELIDED = "[tool result elided to fit the context window; call the tool again if you still need it]"


def compact_history(messages: list[dict[str, Any]], keep_recent: int = 4) -> int:
    """Replace the content of older tool results with a short note; returns how many were elided."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool" and m.get("content") != ELIDED]
    victims = tool_idx[: max(0, len(tool_idx) - keep_recent)] if keep_recent else tool_idx
    for i in victims:
        messages[i]["content"] = ELIDED
    return len(victims)


def review_terminal(changed_lines: dict[str, set[int]]) -> TerminalTool:
    return TerminalTool(
        SUBMIT_REVIEW_TOOL,
        lambda args: parse_review(args, changed_lines),
        lambda r: f"review submitted: {r.verdict}, {len(r.findings)} finding(s)",
    )


async def run_review(
    llm: OpenRouter,
    bridge: ToolBridge,
    system: str,
    opening: str,
    changed_lines: dict[str, set[int]],
    *,
    max_steps: int = 40,
    max_context_tokens: int = 160_000,
    max_rejections: int = 2,
    max_seconds: float = 0,
    extra_tools: list[LocalTool] | None = None,
    log=print,
    quiet: bool = False,
) -> RunResult:
    """The single-pass review: investigate with the tools, then submit_review."""
    res = await run_agent(
        llm,
        bridge,
        system,
        opening,
        review_terminal(changed_lines),
        max_steps=max_steps,
        max_context_tokens=max_context_tokens,
        max_rejections=max_rejections,
        max_seconds=max_seconds,
        extra_tools=extra_tools,
        log=log,
        quiet=quiet,
    )
    return RunResult(
        res.output,
        res.steps,
        res.usage,
        res.models_used,
        res.forced,
        res.errors,
        res.transcript,
        res.seconds,
        res.tool_results,
    )


def _short(args: dict[str, Any], limit: int = 80) -> str:
    s = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return s if len(s) <= limit else s[: limit - 3] + "..."
