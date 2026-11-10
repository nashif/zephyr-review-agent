# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Deliver the review: PR comment, inline review, action outputs, artifacts."""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agent import RunResult
from .config import SEVERITY_RANK, ReviewConfig
from .external import Claim
from .followup import pending_resolutions, reposted_markers, resolve_addressed
from .github_api import GitHub, GitHubError, PullRequest
from .render import inline_comments, inline_marker, marker, posted_title
from .schema import Review

# Called in "ask" mode with the rendered comment and the inline comments; returns
# (post it?, body to post, inline comments to post). The CLI provides an interactive one; a job gets none.
ConfirmFn = Callable[[str, list[dict[str, Any]]], tuple[bool, str, list[dict[str, Any]]]]
# Renders the top-level comment; the argument says whether anchored findings are left to inline comments.
RenderFn = Callable[[bool], str]


def write_outputs(values: dict[str, Any]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in values.items():
            fh.write(f"{k}={v}\n")


def write_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")


def save_artifacts(
    out_dir: str,
    run: RunResult,
    body: str,
    pr: PullRequest,
    cfg: ReviewConfig,
    inline: list[dict[str, Any]] | None = None,
    claims: list[Claim] | None = None,
) -> Path:
    """comment.md is the top-level comment as it would be posted; inline_comments.json the anchored findings;
    claims.json the claims the run verified (with the threads of the earlier comments among them)."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "comment.md").write_text(body, encoding="utf-8")
    (d / "inline_comments.json").write_text(json.dumps(inline or [], indent=2), encoding="utf-8")
    if claims:
        (d / "claims.json").write_text(json.dumps([dataclasses.asdict(c) for c in claims], indent=2), encoding="utf-8")
    record = {
        "pr": pr.number,
        "head_sha": pr.head_sha,
        "base_sha": pr.base_sha,
        "repo": cfg.repo_slug,
        "models": run.models_used,
        "steps": run.steps,
        "forced": run.forced,
        "seconds": round(run.seconds, 1),
        "usage": dataclasses.asdict(run.usage),
        "stages": run.stages,
        "errors": run.errors,
        "review": run.review.to_dict() if run.review else None,
    }
    (d / "review.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if run.review_map is not None:
        (d / "review_map.json").write_text(json.dumps(run.review_map, indent=2), encoding="utf-8")
    if run.review is not None and run.review.test_plan:
        from .testplan import to_yaml

        (d / "test_plan.yaml").write_text(to_yaml(run.review, pr.head_sha), encoding="utf-8")
    with (d / "transcript.jsonl").open("w", encoding="utf-8") as fh:
        for entry in run.transcript:
            fh.write(json.dumps(entry) + "\n")
    return d


def gate_fails(run: RunResult, cfg: ReviewConfig) -> str | None:
    if run.review is None:
        return "review did not complete"
    threshold = SEVERITY_RANK.get(cfg.fail_on_severity, 0)
    if threshold and SEVERITY_RANK.get(run.review.highest_severity(), 0) >= threshold:
        return f"finding at severity {run.review.highest_severity()} (threshold {cfg.fail_on_severity})"
    return None


def publish(
    cfg: ReviewConfig,
    gh: GitHub | None,
    pr: PullRequest,
    run: RunResult,
    render: RenderFn,
    log=print,
    confirm: ConfirmFn | None = None,
    claims: list[Claim] | None = None,
) -> dict[str, Any]:
    """`claims` are what the run verified; those carrying the thread of an earlier inline comment decide
    what happens to that thread: refuted ones are resolved after posting, confirmed ones are not re-posted."""
    outputs: dict[str, Any] = {
        "verdict": run.review.verdict if run.review else "error",
        "findings_count": len(run.review.findings) if run.review else 0,
        "highest_severity": run.review.highest_severity() if run.review else "none",
        "test_plan_count": len(run.review.test_plan) if run.review else 0,
        "comment_url": "",
        "review_url": "",
        "inline_count": 0,
        "resolved_count": 0,
        "posted": "false",
        "skipped": "false",
    }
    inline = inline_comments(run.review, cfg.comment_tag) if (cfg.post_inline and run.review is not None) else []
    if inline and claims and run.review is not None:
        inline = _without_still_open(inline, run.review, claims, cfg.comment_tag, log)
    body = render(bool(inline))
    if gh is None or not pr.number:
        log("no pull request: nothing to post")
        return outputs
    if cfg.publish == "never":
        log("publish=never: not posting to GitHub")
        return outputs
    if cfg.publish == "ask":
        if confirm is None:
            log(
                f"::notice::publish=ask without a terminal: the review was saved, not posted. Read it, then publish "
                f"with: zephyr-reviewer post --output-dir {cfg.output_dir} --repository {cfg.repo_slug}"
            )
            return outputs
        pending = pending_resolutions(run.review, claims) if (claims and run.review is not None) else []
        if pending:
            log(f"{len(pending)} earlier inline comment(s) will be resolved as no longer applying if you post")
        ok, body, inline = confirm(body, inline)
        if not ok:
            log("not posted")
            return outputs
    posted = _post(gh, pr.number, pr.head_sha, body, inline, cfg.post_comment, cfg.comment_tag, log, render)
    outputs.update(posted)
    if claims and run.review is not None:
        resolved = resolve_addressed(gh, pr.number, pr.head_sha, run.review, claims, log, tag=cfg.comment_tag)
        outputs["resolved_count"] = len(resolved)
    return outputs


def _without_still_open(
    inline: list[dict[str, Any]], review: Review, claims: list[Claim], tag: str, log
) -> list[dict[str, Any]]:
    """Drop the inline comments that restate an earlier comment the run confirmed: its thread is still open."""
    skip = reposted_markers(review, claims, tag)
    if not skip:
        return inline
    kept = [c for c in inline if not any(m in c["body"] for m in skip)]
    if len(kept) < len(inline):
        log(f"{len(inline) - len(kept)} finding(s) confirm earlier inline comment(s) still open; not posted again")
    return kept


def _post(
    gh: GitHub,
    number: int,
    head_sha: str,
    body: str,
    inline: list[dict[str, Any]],
    post_comment: bool,
    tag: str,
    log,
    render: RenderFn | None = None,
) -> dict[str, Any]:
    """Top-level comment first (upserted), then the inline review. If GitHub rejects the inline review
    the anchored findings are folded back into the top-level comment so nothing is lost."""
    out: dict[str, Any] = {"comment_url": "", "review_url": "", "inline_count": 0, "posted": "false"}
    if post_comment:
        out["comment_url"] = gh.upsert_comment(number, body, marker(tag))
        out["posted"] = "true"
        log(f"comment: {out['comment_url']}")
    if inline:
        fresh = _without_already_posted(gh, number, inline, tag, log)
        if fresh:
            try:
                out["review_url"] = gh.create_review(
                    number, head_sha, fresh, body=f"{len(fresh)} inline note(s) from the AI review."
                )
                out["inline_count"] = len(fresh)
                out["posted"] = "true"
                log(f"inline review: {out['review_url']} ({len(fresh)} comment(s))")
            except GitHubError as exc:
                log(f"::warning::inline review rejected ({exc}); folding the findings into the summary comment")
                if post_comment and render is not None:
                    out["comment_url"] = gh.upsert_comment(number, render(False), marker(tag))
    return out


def _without_already_posted(
    gh: GitHub, number: int, inline: list[dict[str, Any]], tag: str, log
) -> list[dict[str, Any]]:
    """Drop inline comments already on the pull request from an earlier run: same marker, or the same
    file and title (older markers included the line range, which drifts between runs)."""
    try:
        existing = gh.review_comments(number)
    except GitHubError:
        return inline
    prefix = inline_marker(tag, "")[:-4]  # "<!-- tag:finding:"
    seen_markers: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    for c in existing:
        body = c.get("body") or ""
        if prefix not in body:
            continue
        seen_markers.update(_markers(body, prefix))
        title = posted_title(body)
        if title:
            seen_titles.add((c.get("path") or "", title))
    fresh = []
    for c in inline:
        if any(m in seen_markers for m in _markers(c["body"], prefix)):
            continue
        if (c["path"], posted_title(c["body"])) in seen_titles:
            continue
        fresh.append(c)
    if len(fresh) < len(inline):
        log(f"{len(inline) - len(fresh)} inline comment(s) already posted by an earlier run; skipping them")
    return fresh


def _markers(body: str, prefix: str) -> list[str]:
    out = []
    pos = 0
    while True:
        i = body.find(prefix, pos)
        if i == -1:
            return out
        j = body.find("-->", i)
        if j == -1:
            return out
        out.append(body[i : j + 3])
        pos = j + 3


def load_saved(output_dir: str) -> tuple[dict[str, Any], str]:
    """The record and comment a previous run saved with save_artifacts."""
    d = Path(output_dir)
    record = json.loads((d / "review.json").read_text(encoding="utf-8"))
    body = (d / "comment.md").read_text(encoding="utf-8")
    return record, body


def post_saved(
    output_dir: str,
    gh: GitHub,
    *,
    inline: bool,
    tag: str,
    log=print,
    confirm: ConfirmFn | None = None,
) -> dict[str, Any]:
    """Publish a saved review (comment.md and inline_comments.json, both editable) to its pull request."""
    record, body = load_saved(output_dir)
    number = int(record.get("pr") or 0)
    if not number:
        raise SystemExit(f"{output_dir}/review.json does not belong to a pull request")
    if marker(tag) not in body:
        body = marker(tag) + "\n" + body
    comments: list[dict[str, Any]] = []
    inline_path = Path(output_dir) / "inline_comments.json"
    if inline and inline_path.exists():
        comments = json.loads(inline_path.read_text(encoding="utf-8"))
    if confirm is not None:
        ok, body, comments = confirm(body, comments)
        if not ok:
            log("not posted")
            return {"posted": "false"}
    head = record.get("head_sha") or ""
    out = _post(gh, number, head, body, comments, True, tag, log)
    claims_path = Path(output_dir) / "claims.json"
    if claims_path.exists() and record.get("review"):
        claims = [Claim(**c) for c in json.loads(claims_path.read_text(encoding="utf-8"))]
        review = Review.from_dict(record["review"])
        out["resolved_count"] = len(resolve_addressed(gh, number, head, review, claims, log, tag=tag))
    return out
