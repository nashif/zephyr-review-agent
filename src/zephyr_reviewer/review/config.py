# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Configuration from CLI flags and GitHub Actions ``INPUT_*`` environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .policy import LOCKDOWN_LEVELS, PUBLISH_MODES, Policy

SEVERITY_RANK = {"none": 0, "suggestion": 1, "low": 2, "medium": 3, "high": 4, "critical": 5}


def _env(name: str, default: str = "") -> str:
    v = os.environ.get("INPUT_" + name.upper())
    if v is None:
        v = os.environ.get("ZR_" + name.upper())
    return default if v is None else v


def _bool(v: str | bool | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return v.strip().lower() in ("1", "true", "yes", "on")


def _list(v: str) -> list[str]:
    return [x.strip() for x in v.replace("\n", ",").split(",") if x.strip()]


@dataclass
class ReviewConfig:
    repo_root: str = "."
    repo_slug: str = ""
    pr_number: int | None = None
    head: str = "HEAD"
    base: str = ""
    fetch_missing: bool = True

    openrouter_api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    models: list[str] = field(default_factory=lambda: ["deepseek/deepseek-v4.1-flash"])
    provider: dict | None = None
    pipeline: str = "auto"  # auto | single | two-stage | multi-stage
    draft_models: list[str] = field(default_factory=list)  # two-stage drafts and multi-stage specialists run here
    draft_ensemble: list[str] = field(default_factory=list)  # multi-stage: more draft models, specialists run on each
    verify_max_steps: int = 15
    dossier_chars: int = 40_000
    specialist_max_steps: int = 20
    hard_case_max_steps: int = 12
    max_hard_cases: int = 8
    specialists: list[str] = field(default_factory=list)  # empty: planned from the change
    temperature: float = 0.1
    max_tokens: int = 8000
    max_steps: int = 40
    max_context_tokens: int = 160_000
    model_timeout_seconds: int = 300  # per model request
    tool_timeout_seconds: int = 120  # per MCP tool call
    max_stage_seconds: int = 0  # wall-clock budget per agent run (0 = none); forces submission when exceeded
    max_tool_result_chars: int = 24_000
    inline_diff_chars: int = 60_000
    prefetch_chars: int = 24_000  # 0 disables prefetched context in the opening message
    generate_test_plan: bool = False
    include_ci: bool = True
    lockdown: str = "label"
    include_discussion: bool = True
    include_linked: bool = True
    external_findings_path: str = ""  # a file of findings from a human or another tool, verified by the run
    max_linked_items: int = 8
    max_linked_chars: int = 8_000
    max_discussion_chars: int = 12_000
    language: str = "English"

    profile: str = "zephyr"
    project_context: str = ""

    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    post_comment: bool = True
    post_inline: bool = True
    comment_tag: str = "zephyr-reviewer"
    quiet: bool = False  # log the major steps only, not every model and tool call
    show_run_details: bool = False  # models, tokens, cost and time in the local log only
    post_run_details: bool = False  # ... and in the posted comment and job summary
    show_refuted_candidates: bool = False
    fail_on_severity: str = "none"
    publish: str = "post"  # post | ask | never

    @property
    def dry_run(self) -> bool:
        return self.publish == "never"

    in_process_server: bool = False
    output_dir: str = "review-output"

    skip_labels: list[str] = field(default_factory=lambda: ["no-ai-review", "skip-review"])
    required_labels: list[str] = field(default_factory=list)
    skip_draft: bool = True
    skip_reviewed: str = "never"  # never | any | same-head | same-patch
    resolve_addressed: bool = True  # on a re-review, resolve the earlier run's inline comments this run refutes
    rereview: str = "full"  # full | delta (review what changed since the earlier review, reuse its verdicts)
    full_review_labels: list[str] = field(default_factory=lambda: ["ai-review-full"])
    previous_review: str = ""  # directory holding the earlier run's review.json, instead of its artifact

    @classmethod
    def from_env(cls) -> ReviewConfig:
        cfg = cls()
        cfg.repo_root = _env("repo_root", os.environ.get("GITHUB_WORKSPACE", "."))
        cfg.repo_slug = _env("repository", os.environ.get("GITHUB_REPOSITORY", ""))
        pr = _env("pr_number")
        cfg.pr_number = int(pr) if pr.strip().isdigit() else None
        cfg.head = _env("head", "HEAD")
        cfg.base = _env("base", "")
        cfg.openrouter_api_key = _env("openrouter_api_key", os.environ.get("OPENROUTER_API_KEY", ""))
        cfg.base_url = _env("base_url", cfg.base_url)
        models = [_env("model", cfg.models[0])] + _list(_env("fallback_models"))
        cfg.models = [m for i, m in enumerate(models) if m and m not in models[:i]]
        prov = _env("provider")
        cfg.provider = json.loads(prov) if prov.strip() else None
        drafts = [_env("draft_model")] + _list(_env("draft_fallback_models"))
        cfg.draft_models = [m for i, m in enumerate(drafts) if m and m not in drafts[:i]]
        cfg.draft_ensemble = [m for m in _list(_env("draft_ensemble")) if m not in cfg.draft_models[:1]]
        cfg.pipeline = _env("pipeline", "auto").strip().lower() or "auto"
        cfg.specialist_max_steps = int(_env("specialist_max_steps", str(cfg.specialist_max_steps)))
        cfg.hard_case_max_steps = int(_env("hard_case_max_steps", str(cfg.hard_case_max_steps)))
        cfg.max_hard_cases = int(_env("max_hard_cases", str(cfg.max_hard_cases)))
        cfg.specialists = _list(_env("specialists"))
        cfg.verify_max_steps = int(_env("verify_max_steps", str(cfg.verify_max_steps)))
        cfg.dossier_chars = int(_env("dossier_chars", str(cfg.dossier_chars)))
        cfg.temperature = float(_env("temperature", str(cfg.temperature)))
        cfg.max_tokens = int(_env("max_tokens", str(cfg.max_tokens)))
        cfg.max_steps = int(_env("max_steps", str(cfg.max_steps)))
        cfg.max_context_tokens = int(_env("max_context_tokens", str(cfg.max_context_tokens)))
        cfg.model_timeout_seconds = int(_env("model_timeout_seconds", str(cfg.model_timeout_seconds)))
        cfg.tool_timeout_seconds = int(_env("tool_timeout_seconds", str(cfg.tool_timeout_seconds)))
        cfg.max_stage_seconds = int(_env("max_stage_seconds", str(cfg.max_stage_seconds)))
        cfg.max_tool_result_chars = int(_env("max_tool_result_chars", str(cfg.max_tool_result_chars)))
        cfg.inline_diff_chars = int(_env("inline_diff_chars", str(cfg.inline_diff_chars)))
        cfg.prefetch_chars = int(_env("prefetch_chars", str(cfg.prefetch_chars)))
        cfg.generate_test_plan = _bool(_env("generate_test_plan"), False)
        cfg.include_ci = _bool(_env("include_ci"), True)
        cfg.lockdown = _env("lockdown", "label").strip().lower() or "label"
        cfg.include_discussion = _bool(_env("include_discussion"), True)
        cfg.include_linked = _bool(_env("include_linked_issues"), True)
        cfg.external_findings_path = _env("external_findings", "").strip()
        cfg.max_linked_items = int(_env("max_linked_issues", str(cfg.max_linked_items)))
        cfg.max_linked_chars = int(_env("max_linked_chars", str(cfg.max_linked_chars)))
        cfg.max_discussion_chars = int(_env("max_discussion_chars", str(cfg.max_discussion_chars)))
        cfg.language = _env("language", cfg.language)
        cfg.profile = _env("review_profile", cfg.profile)
        cfg.project_context = _env("project_context")
        cfg.github_token = _env("github_token", os.environ.get("GITHUB_TOKEN", ""))
        cfg.github_api_url = os.environ.get("GITHUB_API_URL", cfg.github_api_url)
        cfg.post_comment = _bool(_env("post_comment"), True)
        cfg.post_inline = _bool(_env("post_inline_comments"), True)
        cfg.comment_tag = _env("comment_tag", cfg.comment_tag)
        cfg.quiet = _bool(_env("quiet"), False)
        cfg.show_run_details = _bool(_env("show_run_details"), False)
        cfg.post_run_details = _bool(_env("post_run_details"), False)
        cfg.show_refuted_candidates = _bool(_env("show_refuted_candidates"), False)
        cfg.fail_on_severity = _env("fail_on_severity", "none").strip().lower() or "none"
        cfg.publish = _env("publish", "post").strip().lower() or "post"
        if _bool(_env("dry_run"), False):
            cfg.publish = "never"
        cfg.output_dir = _env("output_dir", cfg.output_dir)
        cfg.skip_labels = _list(_env("skip_labels", ",".join(cfg.skip_labels)))
        cfg.required_labels = _list(_env("required_labels"))
        cfg.skip_draft = _bool(_env("skip_draft"), True)
        cfg.skip_reviewed = _env("skip_reviewed", "never").strip().lower() or "never"
        cfg.resolve_addressed = _bool(_env("resolve_addressed"), True)
        cfg.rereview = _env("rereview", "full").strip().lower() or "full"
        cfg.full_review_labels = _list(_env("full_review_labels", ",".join(cfg.full_review_labels)))
        cfg.previous_review = _env("previous_review")
        return cfg

    def validate(self) -> list[str]:
        errors = []
        if not self.openrouter_api_key:
            errors.append("OpenRouter API key missing (--api-key, OPENROUTER_API_KEY or INPUT_OPENROUTER_API_KEY)")
        if self.pipeline not in ("auto", "single", "two-stage", "multi-stage"):
            errors.append("pipeline must be auto, single, two-stage or multi-stage")
        if self.publish not in PUBLISH_MODES:
            errors.append(f"publish must be one of {', '.join(PUBLISH_MODES)}")
        if self.lockdown not in LOCKDOWN_LEVELS:
            errors.append(f"lockdown must be one of {', '.join(LOCKDOWN_LEVELS)}")
        if self.fail_on_severity not in SEVERITY_RANK:
            errors.append(f"fail_on_severity must be one of {', '.join(SEVERITY_RANK)}")
        if not self.models:
            errors.append("at least one model is required")
        if self.pr_number is None and not self.base:
            errors.append("either a pull request number or --base is required")
        if self.pr_number is not None and not self.repo_slug:
            errors.append("--repository owner/name is required with a pull request number")
        if self.pr_number is not None and not self.github_token and not self.dry_run:
            errors.append("GitHub token missing (--github-token, GITHUB_TOKEN or INPUT_GITHUB_TOKEN)")
        if self.skip_reviewed not in ("never", "any", "same-head", "same-patch"):
            errors.append(f"skip_reviewed must be never, any, same-head or same-patch, not {self.skip_reviewed!r}")
        if self.rereview not in ("full", "delta"):
            errors.append(f"rereview must be full or delta, not {self.rereview!r}")
        return errors

    def policy(self) -> Policy:
        """The safety floor set by the operator (environment / workflow inputs)."""
        return Policy(
            lockdown=self.lockdown,
            max_steps=self.max_steps,
            max_context_tokens=self.max_context_tokens,
            max_tool_result_chars=self.max_tool_result_chars,
            post_comment=self.post_comment,
            post_inline=self.post_inline,
            publish=self.publish,
        )

    def apply_policy(self, policy: Policy) -> None:
        self.lockdown = policy.lockdown
        self.max_steps = policy.max_steps
        self.max_context_tokens = policy.max_context_tokens
        self.max_tool_result_chars = policy.max_tool_result_chars
        self.post_comment = policy.post_comment
        self.post_inline = policy.post_inline
        self.publish = policy.publish

    def effective_pipeline(self) -> str:
        if self.pipeline != "auto":
            return self.pipeline
        if self.draft_ensemble:
            return "multi-stage"  # an ensemble only means something to the specialist pipeline
        return "two-stage" if self.draft_models else "single"
