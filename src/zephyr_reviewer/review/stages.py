# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Two-stage review: a low-cost model investigates and drafts, a stronger model verifies and finalizes.

The investigation turns dominate cost because the whole transcript is resent on every call. Stage 1
spends them on a cheap model. Stage 2 receives the draft review and a dossier of the evidence stage 1
gathered, keeps tool access with a small budget of its own, and must confirm, correct or drop every
draft finding against the code before submitting. If stage 1 fails to submit, stage 2 runs as a
normal single-stage review so a cheap-model failure never degrades the result.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .agent import LocalTool, RunResult, run_review
from .mcp_client import ToolBridge
from .openrouter import OpenRouter, Usage
from .schema import SUBMIT_TOOL_NAME, Review

# Lookups whose results are worth carrying into stage 2, most valuable first.
_DOSSIER_PRIORITY = [
    "read_file",
    "file_diff",
    "find_definition",
    "kconfig_symbol",
    "dt_binding",
    "find_references",
    "read_file_at_base",
    "related_tests",
    "maintainers",
    "search",
    "blame",
    "pr_discussion",
    "commit_messages",
    "file_history",
    "list_files",
    "project_guidelines",
    "pr_overview",
]


def draft_markdown(review: Review) -> str:
    out = [f"Verdict: {review.verdict}   Confidence: {review.confidence}", "", review.summary, ""]
    if not review.findings:
        out.append("No findings.")
    for i, f in enumerate(review.findings, 1):
        loc = f"{f.file}:{f.line}" + (f"-{f.end_line}" if f.end_line else "") if f.file else "general"
        out += [
            f"#### D{i}. {f.title}",
            f"{f.severity} · {f.category} · {loc} · confidence {f.confidence}",
            f"Problem: {f.problem}",
        ]
        if f.trigger:
            out.append(f"Trigger: {f.trigger}")
        if f.impact:
            out.append(f"Impact: {f.impact}")
        if f.recommendation:
            out.append(f"Recommendation: {f.recommendation}")
        if f.suggested_code.strip():
            out += ["Suggested code:", "```", f.suggested_code.rstrip(), "```"]
        if f.evidence:
            out.append(f"Evidence claimed: {f.evidence}")
        out.append("")
    if review.test_assessment:
        out += ["Test assessment: " + review.test_assessment, ""]
    if review.commit_message_assessment:
        out += ["Commit message assessment: " + review.commit_message_assessment, ""]
    if review.discussion_assessment:
        out += ["Discussion assessment: " + review.discussion_assessment, ""]
    return "\n".join(out)


def build_dossier(tool_results: list[dict[str, Any]], max_chars: int, per_result_chars: int = 8_000) -> str:
    """Deduplicated stage-1 lookups, highest-value first, within a character budget."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for r in tool_results:
        if r["name"] in ("pr_overview", "commit_messages"):
            continue  # stage 2 already has these in its opening message
        key = r["name"] + json.dumps(r.get("arguments") or {}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    rank = {name: i for i, name in enumerate(_DOSSIER_PRIORITY)}
    unique.sort(key=lambda r: rank.get(r["name"], len(rank)))
    parts: list[str] = []
    used = 0
    skipped = 0
    for r in unique:
        args = ", ".join(f"{k}={v!r}" for k, v in (r.get("arguments") or {}).items())
        content = r["content"]
        if len(content) > per_result_chars:
            content = content[:per_result_chars] + "\n... [clipped in dossier; call the tool for the rest]"
        block = f"### {r['name']}({args})\n{content}\n"
        if used + len(block) > max_chars:
            skipped += 1
            continue
        parts.append(block)
        used += len(block)
    if skipped:
        parts.append(f"({skipped} further stage-1 lookup(s) omitted for size; repeat them if needed.)")
    return "\n".join(parts) if parts else "(stage 1 recorded no reusable lookups)"


def verification_message(draft: RunResult, dossier: str, draft_models: list[str]) -> str:
    assert draft.review is not None
    return "\n".join(
        [
            "## Draft review from a first-pass model",
            f"A lower-cost model ({', '.join(draft_models) or 'unknown'}) investigated this pull request first, in "
            f"{draft.steps} step(s) with {draft.tool_call_count} tool call(s). Its draft review follows, then the "
            "evidence it gathered. Treat both as leads, not facts: that model is weaker than you.",
            "",
            "### Draft review",
            draft_markdown(draft.review),
            "",
            "### Evidence gathered by the first pass (tool results, verbatim)",
            dossier,
            "",
            "### Your task",
            "1. For every draft finding, check the cited code yourself (read_file and the lookups above are a start). "
            "Keep it only if you can confirm the problem, the trigger and the impact; correct severity, line and "
            "wording where the draft is imprecise; drop it when it is wrong, speculative, pre-existing, or already "
            "handled by code the draft did not read.",
            "2. Then look for what the first pass missed in the riskiest changed regions (locking and lifetimes, "
            "error paths, Kconfig/devicetree correctness, public API changes). Spend your remaining budget there.",
            "3. Re-check any test, commit message or discussion problem the draft raised; keep it only if real. "
            "Those fields stay empty when there is no problem.",
            f"4. Call `{SUBMIT_TOOL_NAME}` with the final review. Set `evidence` on each finding to what *you* "
            "verified. Do not mention the draft, the first pass, or the staging in the review text.",
        ]
    )


async def run_two_stage(
    draft_llm: OpenRouter,
    verify_llm: OpenRouter,
    bridge: ToolBridge,
    system: str,
    opening: str,
    changed_lines: dict[str, set[int]],
    *,
    max_steps: int = 40,
    verify_max_steps: int = 15,
    max_context_tokens: int = 160_000,
    dossier_chars: int = 40_000,
    extra_tools: list[LocalTool] | None = None,
    verify_system: str | None = None,
    max_seconds: float = 0,
    log=print,
    quiet: bool = False,
) -> RunResult:
    """``system`` drives the draft; ``verify_system`` (default: the same) drives the verifier, and is where
    the false-positive guide and severity calibration belong."""
    started = time.monotonic()
    verify_system = verify_system or system
    log(f"stage 1 (draft): {', '.join(draft_llm.models)}")
    draft = await run_review(
        draft_llm,
        bridge,
        system,
        opening,
        changed_lines,
        max_steps=max_steps,
        max_context_tokens=max_context_tokens,
        max_seconds=max_seconds,
        extra_tools=extra_tools,
        log=lambda m: log("  " + m),
        quiet=quiet,
    )
    stages = [draft.stage_summary("draft")]

    if draft.review is None:
        log(f"stage 1 did not submit ({'; '.join(draft.errors)}); running stage 2 as a full review")
        verify_opening = opening
        verify_steps = max_steps
    else:
        dossier = build_dossier(draft.tool_results, dossier_chars)
        log(
            f"stage 1 drafted {len(draft.review.findings)} finding(s), verdict {draft.review.verdict}; "
            f"dossier {len(dossier):,} chars from {len(draft.tool_results)} lookup(s)"
        )
        verify_opening = opening + "\n\n" + verification_message(draft, dossier, draft.models_used or draft_llm.models)
        verify_steps = verify_max_steps

    log(f"stage 2 (verify): {', '.join(verify_llm.models)}, up to {verify_steps} step(s)")
    final = await run_review(
        verify_llm,
        bridge,
        verify_system,
        verify_opening,
        changed_lines,
        max_steps=verify_steps,
        max_context_tokens=max_context_tokens,
        max_seconds=max_seconds,
        extra_tools=extra_tools,
        log=lambda m: log("  " + m),
        quiet=quiet,
    )
    stages.append(final.stage_summary("verify"))

    usage = Usage()
    for u in (draft.usage, final.usage):
        usage.merge(u)
    models = list(dict.fromkeys([*draft.models_used, *final.models_used]))
    transcript = [{**t, "stage": "draft"} for t in draft.transcript] + [
        {**t, "stage": "verify"} for t in final.transcript
    ]
    errors = list(final.errors)
    if draft.review is None:
        errors = [f"draft stage: {e}" for e in draft.errors] + errors
    return RunResult(
        review=final.review,
        steps=draft.steps + final.steps,
        usage=usage,
        models_used=models,
        forced=final.forced,
        errors=errors,
        transcript=transcript,
        seconds=time.monotonic() - started,
        tool_results=final.tool_results,
        stages=stages,
    )
