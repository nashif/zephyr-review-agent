# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Inline comments this tool posted on an earlier head, re-checked when the pull request moves.

Each open one enters the new run as a claim to verify, like a finding supplied from a file. A claim
the run refutes was fixed or no longer applies: its thread gets a short reply and is resolved. A
confirmed one keeps its thread, and the new finding that confirms it is not posted a second time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .external import EARLIER_REVIEW, MAX_CLAIMS, Claim
from .github_api import GitHub, GitHubError
from .render import ADVISORY_SHORT, _sentence, finding_key, inline_marker, normalize_title
from .schema import Review


@dataclass
class OpenFinding:
    """An inline comment of ours whose thread nobody resolved."""

    comment_id: int
    thread_id: str
    path: str
    line: int
    title: str
    text: str
    severity: str = "medium"
    recommendation: str = ""
    outdated: bool = False
    url: str = ""


_TITLE = re.compile(r"^\*\*(?P<title>.+?)\*\*(?: · (?P<badge>.*))?$")
_SEVERITY = re.compile(r"\b(critical|high|medium|low|suggestion)\b")
_CLAIM_ID = re.compile(r"\bX\d+\b")


def parse_posted(body: str) -> tuple[str, str, str, str]:
    """(title, text, severity, recommendation) of an inline comment this tool posted, read back from
    its markdown; the title is empty when the body is not one of ours."""
    title = severity = recommendation = ""
    text: list[str] = []
    fenced = False
    for raw in body.splitlines():
        ln = raw.strip()
        if ln.startswith("```"):
            fenced = not fenced  # the suggestion block is code, not the finding
            continue
        if fenced or ln.startswith("<!--") or ln.startswith("<sub>"):
            continue
        if not title:
            m = _TITLE.match(ln)
            if m:
                title = m.group("title").strip()
                sev = _SEVERITY.search(m.group("badge") or "")
                severity = sev.group(1) if sev else ""
            continue
        if ln.startswith("**Fix:**"):
            recommendation = ln[len("**Fix:**") :].strip()
            continue
        text.append(ln)
    return title, " ".join(" ".join(text).split()), severity, recommendation


def open_findings(gh: GitHub, number: int, tag: str) -> list[OpenFinding]:
    """This tool's inline comments on the pull request whose threads are still open, oldest first.
    Raises GitHubError when the threads cannot be listed (without them nothing could be resolved)."""
    prefix = inline_marker(tag, "")[:-4]  # "<!-- tag:finding:"
    by_comment: dict[int, dict[str, Any]] = {}
    for t in gh.review_threads(number):
        for cid in t["comment_ids"]:
            if cid is not None:
                by_comment[int(cid)] = t
    out: list[OpenFinding] = []
    for raw in gh.review_comments(number):
        body = raw.get("body") or ""
        if prefix not in body or raw.get("in_reply_to_id"):
            continue
        thread = by_comment.get(int(raw.get("id") or 0))
        if thread is None or thread["isResolved"] or not thread["id"]:
            continue
        title, text, severity, recommendation = parse_posted(body)
        if not title:
            continue
        out.append(
            OpenFinding(
                comment_id=int(raw["id"]),
                thread_id=str(thread["id"]),
                path=raw.get("path") or thread.get("path") or "",
                line=int(raw.get("line") or raw.get("original_line") or thread.get("line") or 0),
                title=title,
                text=text,
                severity=severity or "medium",
                recommendation=recommendation,
                outdated=raw.get("position") is None,
                url=raw.get("html_url") or "",
            )
        )
    return out


def as_claims(findings: list[OpenFinding], start: int = 1, old_head: str = "") -> list[Claim]:
    """The open comments as claims ``X{start}``, ``X{start+1}``, ... carrying their thread for later."""
    source = f"{EARLIER_REVIEW} of {old_head[:12]}" if old_head else EARLIER_REVIEW
    claims: list[Claim] = []
    for i, f in enumerate(findings[:MAX_CLAIMS]):
        extra: dict[str, Any] = {
            "title": f.title,
            "severity": f.severity,
            "thread_id": f.thread_id,
            "comment_id": f.comment_id,
            "url": f.url,
        }
        if f.recommendation:
            extra["recommendation"] = f.recommendation
        text = f"{f.title}: {f.text}" if f.text else f.title
        claims.append(Claim(f"X{start + i}", text, f.path, f.line, source, extra))
    return claims


def is_followup(claim: Claim) -> bool:
    return bool(claim.extra.get("thread_id"))


def reposted_markers(review: Review, claims: list[Claim], tag: str) -> set[str]:
    """Markers of the new findings that confirm an earlier comment whose thread is still open; posting
    them would put the same finding on the pull request twice."""
    confirmed = {v.id for v in review.external_claims if v.verdict == "confirmed"}
    still_open = {c.id: c for c in claims if is_followup(c) and c.id in confirmed}
    if not still_open:
        return set()
    titles = {(c.file, normalize_title(c.title)) for c in still_open.values()}
    out: set[str] = set()
    for f in review.findings:
        cited = set(_CLAIM_ID.findall(f.evidence)) & set(still_open)
        if cited or (f.file, normalize_title(f.title)) in titles:
            out.add(inline_marker(tag, finding_key(f)))
    return out


def pending_resolutions(review: Review, claims: list[Claim]) -> list[tuple[Claim, str]]:
    """(claim, reason) for every earlier comment the run refuted: the threads to resolve."""
    by_id = {c.id: c for c in claims if is_followup(c)}
    out = []
    for v in review.external_claims:
        c = by_id.get(v.id)
        if c is not None and v.verdict == "refuted":
            out.append((c, " ".join((v.reason or "").split())))
    return out


def resolve_addressed(
    gh: GitHub, number: int, head_sha: str, review: Review, claims: list[Claim], log=print
) -> list[dict[str, Any]]:
    """Reply to and resolve the thread of every earlier comment the run refuted; returns what was resolved."""
    done: list[dict[str, Any]] = []
    for c, reason in pending_resolutions(review, claims):
        reason = _sentence(reason)  # the verifier's words, without the run's internal ids
        why = f": {reason}" if reason else "."
        body = f"No longer applies at {head_sha[:12]}{why}\n\n<sub>{ADVISORY_SHORT}</sub>"
        try:
            gh.reply_to_review_comment(number, int(c.extra["comment_id"]), body)
            gh.resolve_thread(str(c.extra["thread_id"]))
        except GitHubError as exc:
            log(f"::warning::could not resolve the thread of {c.id} ({c.location} {c.title}): {exc}")
            continue
        url = c.extra.get("url", "")
        done.append({"id": c.id, "file": c.file, "line": c.line, "title": c.title, "reason": reason, "url": url})
    if done:
        log(
            f"resolved {len(done)} earlier inline comment(s) that no longer apply at {head_sha[:12]}: "
            + "; ".join(f"{d['id']} {d['file']}:{d['line']} {d['title'][:60]}" for d in done)
        )
    return done
