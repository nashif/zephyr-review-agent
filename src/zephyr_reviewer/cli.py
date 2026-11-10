# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""zephyr-reviewer: serve the source-intelligence MCP server, or run a review against it."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from . import mcp_server
from .intel import SourceIntel
from .review import publish as pub
from .review import testplan
from .review.agent import LocalTool, run_review
from .review.ci import CI_FAILURE_LOGS_TOOL, CI_STATUS_TOOL, CIResults
from .review.config import ReviewConfig
from .review.discussion import PR_DISCUSSION_TOOL, Discussion, trust_level
from .review.external import Claim, claims_digest, parse_claims, reconcile
from .review.followup import as_claims, open_findings, retry_resolutions
from .review.github_api import GitHub, GitHubError, PullRequest, pr_number_from_event
from .review.linked import LINKED_ISSUE_TOOL, Linked, load_linked
from .review.mcp_client import ToolBridge, stdio_params
from .review.openrouter import OpenRouter
from .review.pipeline import hunk_text, run_pipeline
from .review.prompts import load_profile, opening_message, system_prompt
from .review.render import (
    head_marker,
    inline_comments,
    marker,
    preview_inline,
    render_comment,
    render_failure,
    run_details,
    run_progress,
)
from .review.stages import run_two_stage


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# Model calls and in-process tool lookups block a worker thread each. The default pool holds cpu_count + 4 threads
# (14 on a 10-core machine), so an ensemble of 9 specialists x 2 models already queues behind it.
WORKER_THREADS = 64


def run_async(coro, threads: int = WORKER_THREADS):
    """asyncio.run with a thread pool wide enough for every stage to have its model call in flight at once."""

    async def main():
        pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="zephyr-reviewer")
        asyncio.get_running_loop().set_default_executor(pool)
        return await coro

    return asyncio.run(main())


# ---------------------------------------------------------------------------- review
def _add_review_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", dest="repo_root", help="Zephyr checkout (default: $GITHUB_WORKSPACE or cwd)")
    p.add_argument("--repository", help="GitHub owner/name (default: $GITHUB_REPOSITORY)")
    p.add_argument("--pr", type=int, help="Pull request number (default: from the Actions event)")
    p.add_argument("--head", help="Commit under review when not using --pr (default: HEAD)")
    p.add_argument("--base", help="Base commit or ref when not using --pr")
    p.add_argument("--model", help="Primary OpenRouter model slug")
    p.add_argument("--fallback-models", help="Comma-separated fallback slugs")
    p.add_argument("--provider", help="OpenRouter provider routing preferences, JSON")
    p.add_argument(
        "--pipeline",
        choices=["auto", "single", "two-stage", "multi-stage"],
        help="auto: two-stage when a draft model is set, else single",
    )
    p.add_argument("--specialists", help="multi-stage: comma-separated specialist passes instead of planning them")
    p.add_argument("--draft-model", help="Low-cost model: drafts in two-stage, runs the specialists in multi-stage")
    p.add_argument("--draft-fallback-models", help="Comma-separated fallbacks for the draft model")
    p.add_argument(
        "--draft-ensemble",
        help="multi-stage: comma-separated extra draft models; every specialist runs once per model and "
        "verification sees how many agreed",
    )
    p.add_argument("--verify-max-steps", type=int, help="Step budget of the verifying model in two-stage mode")
    p.add_argument("--api-key", help="OpenRouter API key (default: $OPENROUTER_API_KEY)")
    p.add_argument("--github-token", help="GitHub token (default: $GITHUB_TOKEN)")
    p.add_argument("--profile", help="Review profile name or path to a .md file (default: zephyr)")
    p.add_argument("--project-context", help="Extra guidance for the model")
    p.add_argument("--max-steps", type=int)
    p.add_argument(
        "--prefetch-chars", type=int, help="Budget for prefetched function context in the opening (0 disables)"
    )
    p.add_argument("--model-timeout", type=int, help="Seconds to wait for one model response (default 300)")
    p.add_argument("--tool-timeout", type=int, help="Seconds to wait for one tool call (default 120)")
    p.add_argument(
        "--max-stage-seconds",
        type=int,
        help="Wall-clock budget per agent run; when exceeded the model is forced to submit (0 = none)",
    )
    p.add_argument("--max-context-tokens", type=int)
    p.add_argument("--test-plan", action="store_true", help="Also produce a test plan: suites, scenarios, platforms")
    p.add_argument(
        "--lockdown",
        choices=["off", "label", "strict"],
        help="Handling of PR text from users without push access (can only tighten the operator setting)",
    )
    p.add_argument("--no-ci", action="store_true", help="Do not fetch CI results")
    p.add_argument("--no-linked", action="store_true", help="Do not fetch issues and PRs the change refers to")
    p.add_argument(
        "--skip-reviewed",
        nargs="?",
        const="any",
        choices=["any", "same-head"],
        help="Skip a PR this tool already commented on: 'any' earlier review, or only when the review was of the "
        "same head commit (default when the flag is given without a value: any)",
    )
    p.add_argument(
        "--findings",
        metavar="FILE",
        help="Findings from a human or another tool (text with numbered items, or JSON) to verify in this run: "
        "each becomes a claim X* the models confirm or refute",
    )
    p.add_argument(
        "--no-resolve-addressed",
        action="store_true",
        help="On a re-review, leave the earlier review's inline comments alone instead of re-checking them "
        "and resolving the ones that no longer apply",
    )
    p.add_argument(
        "--no-discussion",
        action="store_true",
        help="Ignore existing reviews and comments on the PR so they cannot influence the review (testing)",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Log only the major steps (stages and their results, posting), not every model and tool call",
    )
    p.add_argument(
        "--show-details",
        action="store_true",
        help="Log models, steps, tokens, cost and time locally (testing); nothing is posted or put in the job summary",
    )
    p.add_argument(
        "--post-details",
        action="store_true",
        help="Append models, steps, tokens, cost and time to the posted comment and the job summary too",
    )
    p.add_argument(
        "--show-refuted",
        action="store_true",
        help="multi-stage: append a collapsed list of candidates verification refuted, with reasons",
    )
    p.add_argument("--inline", action="store_true", help="Post anchored findings as inline review comments (default)")
    p.add_argument("--no-inline", action="store_true", help="Put every finding in the top-level comment instead")
    p.add_argument("--no-comment", action="store_true", help="Do not post or update the summary comment")
    p.add_argument("--fail-on-severity", choices=["none", "suggestion", "low", "medium", "high", "critical"])
    p.add_argument("--dry-run", action="store_true", help="Run the review but print instead of posting (publish=never)")
    p.add_argument("--ask", action="store_true", help="Show the review and ask before posting (publish=ask)")
    p.add_argument(
        "--publish", choices=["post", "ask", "never"], help="Posting mode; can only tighten the operator setting"
    )
    p.add_argument("--in-process", action="store_true", help="Run the MCP server in-process instead of over stdio")
    p.add_argument("--output-dir", help="Where to write comment.md, review.json, transcript.jsonl")


def _config(args: argparse.Namespace) -> ReviewConfig:
    cfg = ReviewConfig.from_env()
    floor = cfg.policy()
    for attr, key in (
        ("repo_root", "repo_root"),
        ("repository", "repo_slug"),
        ("head", "head"),
        ("base", "base"),
        ("api_key", "openrouter_api_key"),
        ("github_token", "github_token"),
        ("profile", "profile"),
        ("project_context", "project_context"),
        ("fail_on_severity", "fail_on_severity"),
        ("output_dir", "output_dir"),
    ):
        v = getattr(args, attr, None)
        if v is not None:
            setattr(cfg, key, v)
    if args.pr is not None:
        cfg.pr_number = args.pr
    elif cfg.pr_number is None and not args.base:
        cfg.pr_number = pr_number_from_event()
    if args.model:
        cfg.models = [args.model] + [m for m in cfg.models[1:] if m != args.model]
    if args.fallback_models:
        cfg.models = [cfg.models[0]] + [m.strip() for m in args.fallback_models.split(",") if m.strip()]
    if args.provider:
        cfg.provider = json.loads(args.provider)
    if args.pipeline:
        cfg.pipeline = args.pipeline
    if args.specialists:
        cfg.specialists = [x.strip() for x in args.specialists.split(",") if x.strip()]
    if args.draft_model:
        cfg.draft_models = [args.draft_model]
    if args.draft_fallback_models:
        cfg.draft_models = cfg.draft_models[:1] + [
            m.strip() for m in args.draft_fallback_models.split(",") if m.strip()
        ]
    if args.draft_ensemble:
        cfg.draft_ensemble = [m.strip() for m in args.draft_ensemble.split(",") if m.strip()]
    if args.verify_max_steps is not None:
        cfg.verify_max_steps = args.verify_max_steps
    if args.test_plan:
        cfg.generate_test_plan = True
    if args.no_ci:
        cfg.include_ci = False
    if args.no_linked:
        cfg.include_linked = False
    if args.no_discussion:
        cfg.include_discussion = False
    if args.findings:
        cfg.external_findings_path = args.findings
    if args.no_resolve_addressed:
        cfg.resolve_addressed = False
    if args.skip_reviewed:
        cfg.skip_reviewed = args.skip_reviewed
    if args.quiet:
        cfg.quiet = True
    if args.show_details:
        cfg.show_run_details = True
    if args.post_details:
        cfg.post_run_details = True
    if args.show_refuted:
        cfg.show_refuted_candidates = True
    if args.in_process:
        cfg.in_process_server = True
    if args.prefetch_chars is not None:
        cfg.prefetch_chars = args.prefetch_chars
    if args.model_timeout is not None:
        cfg.model_timeout_seconds = args.model_timeout
    if args.tool_timeout is not None:
        cfg.tool_timeout_seconds = args.tool_timeout
    if args.max_stage_seconds is not None:
        cfg.max_stage_seconds = args.max_stage_seconds
    # Safety settings: the environment (workflow inputs) is the operator floor; flags may only tighten it.
    policy, rejected = floor.tighten(
        lockdown=args.lockdown,
        max_steps=args.max_steps,
        max_context_tokens=args.max_context_tokens,
        post_inline=False if args.no_inline else (True if args.inline else None),
        post_comment=False if args.no_comment else None,
        publish="never" if args.dry_run else ("ask" if args.ask else args.publish),
    )
    for r in rejected:
        log(f"::warning::ignored: {r}")
    cfg.apply_policy(policy)
    return cfg


def _ensure_commit(repo_root: str, sha: str, hint_ref: str | None) -> None:
    """Make sure ``sha`` is present locally, fetching it by SHA (or via a ref) when it is not."""
    if (
        subprocess.run(["git", "-C", repo_root, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode
        == 0
    ):
        return
    log(f"fetching {sha[:12]} ...")
    attempts = [["git", "-C", repo_root, "fetch", "--no-tags", "origin", sha]]
    if hint_ref:
        attempts.append(["git", "-C", repo_root, "fetch", "--no-tags", "origin", hint_ref])
    for cmd in attempts:
        if (
            subprocess.run(cmd, capture_output=True).returncode == 0
            and subprocess.run(
                ["git", "-C", repo_root, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
            ).returncode
            == 0
        ):
            return
    raise SystemExit(f"commit {sha} is not available in {repo_root} and could not be fetched")


def _resolve_pr(cfg: ReviewConfig) -> tuple[PullRequest, GitHub | None]:
    if cfg.pr_number is None:
        intel_head = subprocess.run(
            ["git", "-C", cfg.repo_root, "rev-parse", cfg.head], capture_output=True, text=True
        ).stdout.strip()
        intel_base = subprocess.run(
            ["git", "-C", cfg.repo_root, "rev-parse", cfg.base], capture_output=True, text=True
        ).stdout.strip()
        subject = subprocess.run(
            ["git", "-C", cfg.repo_root, "log", "-1", "--format=%s", cfg.head], capture_output=True, text=True
        ).stdout.strip()
        pr = PullRequest(
            number=0,
            title=subject or cfg.head,
            body="",
            author="",
            base_ref=cfg.base,
            base_sha=intel_base,
            head_ref=cfg.head,
            head_sha=intel_head,
            head_repo="",
            draft=False,
        )
        return pr, None
    gh = GitHub(cfg.github_token, cfg.repo_slug, cfg.github_api_url)
    pr = gh.pull(cfg.pr_number)
    merge_base = gh.merge_base(pr.base_sha, pr.head_sha) or pr.base_sha
    if cfg.fetch_missing:
        _ensure_commit(cfg.repo_root, pr.head_sha, f"pull/{pr.number}/head")
        _ensure_commit(cfg.repo_root, merge_base, pr.base_ref)
    pr.base_sha = merge_base
    cfg.head, cfg.base = pr.head_sha, merge_base
    return pr, gh


def _skip_reason(cfg: ReviewConfig, pr: PullRequest, gh: GitHub | None = None) -> str | None:
    if pr.number == 0:
        return None
    if cfg.skip_draft and pr.draft:
        return "pull request is a draft"
    labels = {lab.lower() for lab in pr.labels}
    hit = labels & {lab.lower() for lab in cfg.skip_labels}
    if hit:
        return f"label {sorted(hit)[0]!r} present"
    if cfg.required_labels and not labels & {lab.lower() for lab in cfg.required_labels}:
        return f"none of the required labels {cfg.required_labels} present"
    if cfg.skip_reviewed != "never" and gh is not None:
        existing = gh.find_comment(pr.number, marker(cfg.comment_tag))
        if existing:
            url = existing.get("html_url") or f"comment {existing.get('id')}"
            if cfg.skip_reviewed == "any":
                return f"already reviewed ({url})"
            if head_marker(cfg.comment_tag, pr.head_sha) in (existing.get("body") or ""):
                return f"already reviewed at this head {pr.head_sha[:12]} ({url})"
            log(f"earlier review found ({url}) but the head moved since; reviewing again")
    return None


def _load_discussion(cfg: ReviewConfig, gh: GitHub | None, pr: PullRequest) -> Discussion | None:
    if gh is not None and pr.number and not cfg.include_discussion:
        log("existing reviews and comments ignored (include_discussion=false)")
        return None
    if gh is None or not pr.number or not cfg.include_discussion:
        return None
    d = Discussion.fetch(gh, pr.number, f"<!-- {cfg.comment_tag} -->", lockdown=cfg.lockdown, pr_author=pr.author)
    withheld = sum(1 for c in [*d.conversation, *d.inline, *d.reviews] if c.withheld)
    log(
        f"discussion: {len(d.conversation)} comment(s), {len(d.inline)} inline, {len(d.reviews)} review(s)"
        f"{', previous AI review present' if d.previous_ai else ''}"
        f"{', thread resolution known' if d.resolution_known else ''}"
        f"{f', {withheld} withheld (strict lockdown)' if withheld else ''}"
    )
    return d


def _load_linked(
    cfg: ReviewConfig, gh: GitHub | None, pr: PullRequest, intel: SourceIntel, discussion: Discussion | None
) -> Linked | None:
    """Issues and PRs referenced by the title, body, commit messages and discussion."""
    if gh is None or not pr.number or not cfg.include_linked:
        return None
    texts: list[tuple[str, str]] = [("title", pr.title), ("description", pr.body)]
    texts += [("commit " + c.short, c.subject + "\n" + c.body) for c in intel.commits]
    if discussion is not None:
        texts += [("comment", c.body) for c in [*discussion.conversation, *discussion.inline, *discussion.reviews]]
    linked = load_linked(
        gh, texts, repo_slug=cfg.repo_slug, own_number=pr.number, max_items=cfg.max_linked_items, lockdown=cfg.lockdown
    )
    if linked.empty:
        log("linked issues/PRs: none referenced")
        return None
    log("linked issues/PRs: " + ", ".join(f"{it.ref.label} ({it.status or it.kind})" for it in linked.items))
    return linked


def _load_ci(cfg: ReviewConfig, gh: GitHub | None, pr: PullRequest) -> CIResults | None:
    if gh is None or not pr.number or not cfg.include_ci:
        return None
    ci = CIResults(gh, pr.head_sha, max_chars=cfg.max_tool_result_chars)
    log(ci.summary_line())
    return ci


async def _run(
    cfg: ReviewConfig,
    pr: PullRequest,
    intel: SourceIntel,
    discussion: Discussion | None = None,
    ci: CIResults | None = None,
    author_trust: str = "",
    linked: Linked | None = None,
    claims: list[Claim] | None = None,
):
    profile = load_profile(cfg.profile)
    mode = cfg.effective_pipeline()
    # The single agent applies the guides itself; staged pipelines apply them where findings are confirmed.
    system = system_prompt(profile, cfg.language, include_guides=(mode == "single"), discussion=cfg.include_discussion)
    verify_system = system_prompt(profile, cfg.language, include_guides=True, discussion=cfg.include_discussion)
    overview = intel.overview()
    diff = intel.diff_text if len(intel.diff_text) <= cfg.inline_diff_chars else None
    digest = discussion.digest(cfg.max_discussion_chars) if discussion else None
    prefetched = intel.prefetch(cfg.prefetch_chars) if cfg.prefetch_chars > 0 else ""
    if prefetched:
        log(f"prefetched {len(prefetched):,} chars of function context")
    opening = opening_message(
        pr,
        overview,
        diff,
        cfg.project_context,
        cfg.repo_slug,
        digest,
        cfg.generate_test_plan,
        author_trust=author_trust,
        ci_summary=ci.summary_line() if ci else "",
        prefetched=prefetched,
        linked=linked.digest(cfg.max_linked_chars) if linked else None,
        claims=claims_digest(claims) if claims else None,
        claims_source=", ".join(dict.fromkeys(c.source for c in claims or [] if c.source)),
    )
    extra_tools = [LocalTool(PR_DISCUSSION_TOOL, discussion.tool_handler)] if discussion else []
    if linked:
        extra_tools.append(LocalTool(LINKED_ISSUE_TOOL, linked.tool_handler))
    if ci:
        extra_tools += [LocalTool(CI_STATUS_TOOL, ci.status_tool), LocalTool(CI_FAILURE_LOGS_TOOL, ci.logs_tool)]
    changed = {f.path: f.new_side_lines for f in intel.files}
    llm = OpenRouter(
        cfg.openrouter_api_key,
        cfg.models,
        base_url=cfg.base_url,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        provider=cfg.provider,
        timeout=cfg.model_timeout_seconds,
        on_event=log,
    )
    if cfg.in_process_server:
        target = mcp_server.build_server(intel)
    else:
        target = stdio_params(cfg.repo_root, cfg.head, cfg.base, cfg.max_tool_result_chars)
    async with ToolBridge(
        target, max_result_chars=cfg.max_tool_result_chars, call_timeout=float(cfg.tool_timeout_seconds)
    ) as bridge:
        log(f"MCP server up with {len(bridge.tools)} tool(s); models: {', '.join(cfg.models)}")
        # The models that draft (two-stage) or run the specialists (multi-stage): draft_model first, then the
        # ensemble. With an ensemble and no draft_model, the first ensemble member drafts; the verification model
        # never runs a specialist pass, otherwise it would be judging its own candidates.
        drafters = cfg.draft_models[:1] + [m for m in cfg.draft_ensemble if m not in cfg.draft_models[:1]]
        fallbacks = cfg.draft_models[1:]

        def drafter(model: str) -> OpenRouter:
            return OpenRouter(
                cfg.openrouter_api_key,
                [model, *[f for f in fallbacks if f != model]],
                base_url=cfg.base_url,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                provider=cfg.provider,
                timeout=cfg.model_timeout_seconds,
                on_event=log,
            )

        cheap_llm = drafter(drafters[0]) if drafters else llm
        if cfg.draft_ensemble and mode != "multi-stage":
            log(f"draft_ensemble is only used by the multi-stage pipeline; ignored in {mode}")
        if mode == "multi-stage":
            ensemble = [drafter(m) for m in drafters[1:]]
            run = await run_pipeline(
                cheap_llm,
                llm,
                bridge,
                system,
                opening,
                changed,
                changed_files=[f.path for f in intel.files],
                added_text=hunk_text(intel.diff_text),
                specialists=cfg.specialists or None,
                specialist_max_steps=cfg.specialist_max_steps,
                verify_max_steps=cfg.verify_max_steps,
                hard_case_max_steps=cfg.hard_case_max_steps,
                max_hard_cases=cfg.max_hard_cases,
                ensemble=ensemble,
                claims=claims,
                max_context_tokens=cfg.max_context_tokens,
                max_seconds=cfg.max_stage_seconds,
                extra_tools=extra_tools,
                log=log,
                quiet=cfg.quiet,
            )
        elif mode == "two-stage":
            run = await run_two_stage(
                cheap_llm,
                llm,
                bridge,
                system,
                opening,
                changed,
                max_steps=cfg.max_steps,
                verify_max_steps=cfg.verify_max_steps,
                max_context_tokens=cfg.max_context_tokens,
                dossier_chars=cfg.dossier_chars,
                extra_tools=extra_tools,
                verify_system=verify_system,
                max_seconds=cfg.max_stage_seconds,
                log=log,
                quiet=cfg.quiet,
            )
        else:
            run = await run_review(
                llm,
                bridge,
                system,
                opening,
                changed,
                max_steps=cfg.max_steps,
                max_context_tokens=cfg.max_context_tokens,
                max_seconds=cfg.max_stage_seconds,
                extra_tools=extra_tools,
                log=log,
                quiet=cfg.quiet,
            )
    if claims and mode != "multi-stage" and run.review is not None:
        # The single agent reports verdicts itself; fill in the claim text and mark the ones it skipped.
        reconcile(run.review, claims, [asdict(v) for v in run.review.external_claims])
    return run


def _load_claims(cfg: ReviewConfig) -> list[Claim] | None:
    """The claims in the findings file, [] when none is configured, None when the file cannot be read."""
    if not cfg.external_findings_path:
        return []
    path = cfg.external_findings_path
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as exc:
        log(f"::error::cannot read findings file {path}: {exc}")
        return None
    claims = parse_claims(text, source=os.path.basename(path))
    if not claims:
        log(f"findings file {path} contains no claims")
    else:
        log(f"{len(claims)} claim(s) to verify from {path}: " + "; ".join(f"{c.id} {c.title[:60]}" for c in claims))
    return claims


def _load_followups(cfg: ReviewConfig, gh: GitHub | None, pr: PullRequest, start: int) -> list[Claim]:
    """The earlier review's open inline comments as claims to re-check, when the head moved since."""
    if gh is None or not pr.number or not cfg.resolve_addressed:
        return []
    existing = gh.find_comment(pr.number, marker(cfg.comment_tag))
    if not existing:
        return []
    body = existing.get("body") or ""
    if head_marker(cfg.comment_tag, pr.head_sha) in body:
        return []  # same head: the code did not change, the comments stand
    m = re.search(rf"<!-- {re.escape(cfg.comment_tag)}:head:([0-9a-f]+) -->", body)
    try:
        found = open_findings(gh, pr.number, cfg.comment_tag)
    except GitHubError as exc:
        log(f"::warning::cannot list the earlier review's inline comments: {exc}")
        return []
    answered = [f for f in found if f.answered]
    if answered:
        if cfg.publish == "post":
            retry_resolutions(gh, answered, log)
        else:
            log(f"{len(answered)} thread(s) answered by an earlier run are still open (publish={cfg.publish})")
    claims = as_claims(found, start, m.group(1) if m else "")
    if claims:
        log(
            f"{len(claims)} open inline comment(s) from the earlier review re-checked as "
            f"{claims[0].id}..{claims[-1].id}; one this run refutes is resolved"
        )
    return claims


def cmd_review(args: argparse.Namespace) -> int:
    cfg = _config(args)
    errors = cfg.validate()
    if errors:
        for e in errors:
            log(f"::error::{e}")
        return 2
    pr, gh = _resolve_pr(cfg)
    reason = _skip_reason(cfg, pr, gh)
    if reason:
        log(f"skipping review: {reason}")
        pub.write_outputs({"skipped": "true", "verdict": "skipped", "findings_count": 0, "highest_severity": "none"})
        return 0
    intel = SourceIntel(cfg.repo_root, cfg.head, cfg.base, max_chars=cfg.max_tool_result_chars)
    if not intel.files:
        log("nothing to review: empty diff")
        pub.write_outputs({"skipped": "true", "verdict": "skipped", "findings_count": 0, "highest_severity": "none"})
        return 0
    log(f"reviewing {len(intel.files)} file(s), {len(intel.commits)} commit(s): {cfg.base[:12]}..{cfg.head[:12]}")
    author_trust = trust_level(gh, pr.author, False) if gh is not None and cfg.lockdown != "off" and pr.author else ""
    claims = _load_claims(cfg)
    if claims is None:
        return 2  # a configuration error, like a bad flag: nothing was reviewed
    claims = claims + _load_followups(cfg, gh, pr, len(claims) + 1)
    try:
        discussion = _load_discussion(cfg, gh, pr)
        run = run_async(
            _run(
                cfg,
                pr,
                intel,
                discussion,
                _load_ci(cfg, gh, pr),
                author_trust,
                _load_linked(cfg, gh, pr, intel, discussion),
                claims,
            )
        )
    except Exception as exc:  # noqa: BLE001 - report, save what we can, exit non-zero
        from .review.agent import RunResult
        from .review.openrouter import Usage

        run = RunResult(None, 0, Usage(), [], False, errors=[f"{type(exc).__name__}: {str(exc)[:800]}"])
        log(f"::error::review failed: {run.errors[0]}")
    if run.review is not None and run.review.test_plan:
        testplan.annotate(run.review, intel)
        flagged = sum(1 for e in run.review.test_plan if e.notes)
        log(f"test plan: {len(run.review.test_plan)} entries, {flagged} flagged by validation")
    if run.review is not None:
        review = run.review

        def render(exclude_inline: bool) -> str:
            return render_comment(
                review,
                run,
                pr,
                repo=cfg.repo_slug,
                tag=cfg.comment_tag,
                api_url=cfg.github_api_url,
                details=cfg.post_run_details,
                exclude_inline=exclude_inline,
                refuted=cfg.show_refuted_candidates,
            )

        inline = inline_comments(review, cfg.comment_tag) if cfg.post_inline else []
    else:
        failure_body = render_failure(run, pr, cfg.comment_tag)
        log("::error::" + "; ".join(run.errors))

        def render(exclude_inline: bool) -> str:
            return failure_body

        inline = []
    body = render(bool(inline))
    out_dir = pub.save_artifacts(cfg.output_dir, run, body, pr, cfg, inline, claims)
    log(f"artifacts in {out_dir}" + (f" ({len(inline)} inline comment(s))" if inline else ""))
    confirm = _interactive_confirm(cfg.output_dir, pr) if cfg.publish == "ask" and _has_terminal() else None
    outputs = pub.publish(cfg, gh, pr, run, render, log=log, confirm=confirm, claims=claims)
    pub.write_outputs(outputs)
    # Models, tokens and cost appear nowhere by default; review.json keeps them for whoever opens the artifact.
    # show_run_details puts them in the local log for testing; only post_run_details lets them reach GitHub
    # (the comment and the job summary).
    pub.write_summary(body + ("\n\n" + run_details(run, pr) if cfg.post_run_details else ""))
    log(run_details(run, pr) if cfg.show_run_details or cfg.post_run_details else run_progress(run))
    if outputs.get("posted") != "true" and confirm is None:
        print(body)
        if inline:
            print("\n" + preview_inline(inline))
    failure = pub.gate_fails(run, cfg)
    if failure:
        log(f"::error::review gate failed: {failure}")
        return 1
    return 0


def _has_terminal() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _interactive_confirm(output_dir: str, pr: PullRequest):
    """Show the summary comment and every inline comment; ask to post, skip, or edit either file first."""
    comment_path = os.path.join(output_dir, "comment.md")
    inline_path = os.path.join(output_dir, "inline_comments.json")

    def edit(path: str) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        subprocess.call([*editor.split(), path])

    def confirm(body: str, inline: list[dict]) -> tuple[bool, str, list[dict]]:
        while True:
            print("\n" + body + "\n", file=sys.stderr)
            if inline:
                print(preview_inline(inline) + "\n", file=sys.stderr)
            what = "the summary comment" + (f" and {len(inline)} inline comment(s)" if inline else "")
            target = f"PR #{pr.number}" if pr.number else "the pull request"
            choices = "[y]es / [n]o / [e]dit comment.md" + (" / [i] edit inline_comments.json" if inline else "")
            try:
                answer = input(f"Post {what} to {target}? {choices}: ").strip().lower()
            except EOFError:
                return False, body, inline
            if answer in ("y", "yes"):
                return True, body, inline
            if answer in ("n", "no", ""):
                return False, body, inline
            if answer in ("e", "edit"):
                edit(comment_path)
                with open(comment_path, encoding="utf-8") as fh:
                    body = fh.read()
                continue
            if answer in ("i", "inline") and inline:
                edit(inline_path)
                try:
                    with open(inline_path, encoding="utf-8") as fh:
                        inline = json.load(fh)
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"inline_comments.json could not be read ({exc}); keeping the previous list", file=sys.stderr)
                continue
            print("please answer y, n, e" + (" or i" if inline else ""), file=sys.stderr)

    return confirm


def cmd_post(args: argparse.Namespace) -> int:
    repo = args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    record, _ = pub.load_saved(args.output_dir)
    repo = repo or record.get("repo") or ""
    token = args.github_token or os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        log("::error::a repository (--repository or review.json) and a GitHub token are required")
        return 2
    gh = GitHub(token, repo, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    pr = PullRequest(int(record.get("pr") or 0), "", "", "", "", "", "", record.get("head_sha") or "", "", False)
    confirm = None if args.yes else (_interactive_confirm(args.output_dir, pr) if _has_terminal() else None)
    if confirm is None and not args.yes:
        log("::error::no terminal to confirm on; pass --yes to post without asking")
        return 2
    out = pub.post_saved(args.output_dir, gh, inline=args.inline, tag=args.comment_tag, log=log, confirm=confirm)
    return 0 if out.get("posted") == "true" else 1


# ------------------------------------------------------------------------- benchmark
def cmd_benchmark_mine(args: argparse.Namespace) -> int:
    from .benchmark import mine as bm

    entries = bm.mine(
        args.repo,
        args.range,
        since=args.since,
        max_entries=args.max,
        use_blame=not args.trailers_only,
        min_confidence=args.min_confidence,
        log=log,
    )
    bm.save(entries, args.out)
    by_source = {}
    for e in entries:
        by_source[e.source] = by_source.get(e.source, 0) + 1
    log(f"{len(entries)} entries written to {args.out} ({', '.join(f'{k} {v}' for k, v in sorted(by_source.items()))})")
    return 0


def cmd_benchmark_run(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .benchmark import mine as bm
    from .benchmark.run import run_benchmark
    from .review.render import run_details  # noqa: F401 - keeps import graph explicit

    cfg = ReviewConfig.from_env()
    cfg.repo_root = args.repo
    if args.model:
        cfg.models = [args.model] + [m for m in cfg.models[1:] if m != args.model]
    if args.draft_model:
        cfg.draft_models = [args.draft_model]
    if args.pipeline:
        cfg.pipeline = args.pipeline
    if args.max_steps is not None:
        cfg.max_steps = args.max_steps
    if args.profile:
        cfg.profile = args.profile
    if args.api_key:
        cfg.openrouter_api_key = args.api_key
    cfg.publish = "never"
    if not args.analyze_only and not cfg.openrouter_api_key:
        log("::error::OpenRouter API key missing (--api-key or OPENROUTER_API_KEY)")
        return 2
    entries = bm.load(args.file)
    if args.subsystem:
        entries = [e for e in entries if args.subsystem.lower() in e.subsystem.lower()]
    if args.source:
        entries = [e for e in entries if e.source == args.source]
    if args.min_confidence:
        entries = [e for e in entries if e.confidence >= args.min_confidence]
    entries = entries[args.offset : args.offset + args.limit if args.limit else None]
    if not entries:
        log("::error::no entries selected")
        return 2
    if args.analyze_only:
        out_dir = Path(args.analyze_only)
    else:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(args.output_dir) / f"{stamp}_{Path(args.file).stem}"
    judge_llm = None
    if not args.no_judge:
        judge_models = [args.judge_model] if args.judge_model else cfg.models
        if not cfg.openrouter_api_key:
            log("::error::judging needs an OpenRouter API key; pass --no-judge to skip")
            return 2
        judge_llm = OpenRouter(
            cfg.openrouter_api_key,
            judge_models,
            base_url=cfg.base_url,
            temperature=0.0,
            max_tokens=1000,
            provider=cfg.provider,
            timeout=cfg.model_timeout_seconds,
            on_event=log,
        )
    log(f"benchmark: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} -> {out_dir}")
    report = run_async(
        run_benchmark(
            cfg,
            args.repo,
            entries,
            out_dir,
            judge_llm=judge_llm,
            concurrency=args.concurrency,
            analyze_only=bool(args.analyze_only),
            log=log,
        ),
        threads=min(256, WORKER_THREADS * max(1, int(args.concurrency or 1))),
    )
    print((out_dir / "summary.md").read_text(encoding="utf-8"))
    det = report["summary"]["detection"]
    log(
        f"detected {det['DETECTED']}, partial {det['PARTIALLY_DETECTED']}, missed {det['MISSED']}; results in {out_dir}"
    )
    return 0


# ----------------------------------------------------------------------------- tools
def cmd_tools(args: argparse.Namespace) -> int:
    intel = SourceIntel(args.repo, args.head, args.base)
    srv = mcp_server.build_server(intel)

    async def go():
        async with ToolBridge(srv) as bridge:
            for t in bridge.tools:
                fn = t["function"]
                params = ", ".join(fn["parameters"].get("properties", {}))
                print(f"{fn['name']}({params})\n    {fn['description'].splitlines()[0]}")

    asyncio.run(go())
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    intel = SourceIntel(args.repo, args.head, args.base)
    srv = mcp_server.build_server(intel)
    kwargs = {}
    for item in args.args:
        k, _, v = item.partition("=")
        if v.isdigit():
            kwargs[k] = int(v)
        elif v.lower() in ("true", "false"):
            kwargs[k] = v.lower() == "true"
        else:
            kwargs[k] = v

    async def go():
        async with ToolBridge(srv, max_result_chars=10**7) as bridge:
            print(await bridge.call(args.tool, kwargs))

    asyncio.run(go())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zephyr-reviewer", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the source-intelligence MCP server")
    mcp_server.add_server_args(s)
    s.set_defaults(fn=mcp_server.serve)

    r = sub.add_parser("review", help="Review a pull request (or a local range) and publish the result")
    _add_review_args(r)
    r.set_defaults(fn=cmd_review)

    o = sub.add_parser("post", help="Publish a review saved by an earlier run (after reading or editing comment.md)")
    o.add_argument("--output-dir", default="review-output", help="Directory holding review.json and comment.md")
    o.add_argument("--repository", help="GitHub owner/name (default: from review.json or $GITHUB_REPOSITORY)")
    o.add_argument("--github-token", help="GitHub token (default: $GITHUB_TOKEN)")
    o.add_argument("--inline", action="store_true", help="Also post anchored findings as inline review comments")
    o.add_argument("--comment-tag", default="zephyr-reviewer")
    o.add_argument("--yes", action="store_true", help="Post without asking")
    o.set_defaults(fn=cmd_post)

    b = sub.add_parser("benchmark", help="Measure the reviewer against bugs the project fixed later")
    bsub = b.add_subparsers(dest="bcmd", required=True)
    bm = bsub.add_parser("mine", help="Build a dataset from fix commits (Fixes: trailers, or blame of removed lines)")
    bm.add_argument("--repo", default=".")
    bm.add_argument("--range", default="HEAD", help="Revision range to scan for fix commits (default: HEAD)")
    bm.add_argument("--since", help="Only fix commits after this date, e.g. 2025-01-01")
    bm.add_argument("--max", type=int, default=200, help="Maximum entries")
    bm.add_argument("--trailers-only", action="store_true", help="Only use Fixes: <sha> trailers, no blame inference")
    bm.add_argument("--min-confidence", type=float, default=0.6, help="Minimum blame share for inferred culprits")
    bm.add_argument("--out", default="benchmark.json")
    bm.set_defaults(fn=cmd_benchmark_mine)
    br = bsub.add_parser("run", help="Review each entry's commit as if new, judge the findings, and report")
    br.add_argument("file", help="Dataset produced by `benchmark mine`")
    br.add_argument("--repo", default=".")
    br.add_argument("--output-dir", default="benchmarks/runs")
    br.add_argument("--analyze-only", metavar="RUN_DIR", help="Re-judge a previous run directory instead of reviewing")
    br.add_argument("--limit", type=int, default=0)
    br.add_argument("--offset", type=int, default=0)
    br.add_argument("--subsystem", help="Only entries whose subsystem contains this text")
    br.add_argument("--source", choices=["trailer", "blame", "manual"])
    br.add_argument("--min-confidence", type=float, default=0.0)
    br.add_argument("--concurrency", type=int, default=2)
    br.add_argument("--model")
    br.add_argument("--draft-model")
    br.add_argument("--pipeline", choices=["auto", "single", "two-stage", "multi-stage"])
    br.add_argument("--judge-model", help="Model that grades detections (default: the review model)")
    br.add_argument("--no-judge", action="store_true")
    br.add_argument("--max-steps", type=int)
    br.add_argument("--profile")
    br.add_argument("--api-key")
    br.set_defaults(fn=cmd_benchmark_run)

    t = sub.add_parser("tools", help="List the MCP tools as the model sees them")
    t.add_argument("--repo", default=".")
    t.add_argument("--head", default="HEAD")
    t.add_argument("--base", required=True)
    t.set_defaults(fn=cmd_tools)

    c = sub.add_parser("call", help="Invoke one MCP tool locally, e.g. call find_definition name=k_sem_take")
    c.add_argument("--repo", default=".")
    c.add_argument("--head", default="HEAD")
    c.add_argument("--base", required=True)
    c.add_argument("tool")
    c.add_argument("args", nargs="*", help="key=value arguments")
    c.set_defaults(fn=cmd_call)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
