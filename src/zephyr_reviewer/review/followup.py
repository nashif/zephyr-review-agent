# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Inline comments this tool posted on an earlier head, re-checked when the pull request moves.

Each open one enters the new run as a claim to verify, like a finding supplied from a file. A claim
the run refutes was fixed or no longer applies: its thread gets a short reply and is resolved. A
confirmed one keeps its thread, and the new finding that confirms it is not posted a second time.

What people wrote in a thread travels with the claim: the author explaining why the code is as it
is, or a reviewer disputing the comment. It is data for the re-check, which may find the reply
right (the claim is refuted on the code that bears it out) or wrong (the claim is confirmed, and
the thread gets an answer saying what the reply overlooks). A reply is never the verdict.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .discussion import sanitize
from .external import EARLIER_REVIEW, MAX_CLAIMS, Claim
from .github_api import GitHub, GitHubError
from .render import ADVISORY_SHORT, _sentence, finding_key, finding_text, inline_marker, normalize_title, posted
from .schema import Finding, Review

MAX_REPLIES = 5  # per thread, the latest ones
MAX_REPLY_CHARS = 1_500


@dataclass
class Reply:
    """What a person wrote in the thread of one of our comments."""

    author: str
    body: str
    role: str = ""  # "author" when written by the pull request's author, else "reviewer"
    trust: str = ""  # maintainer, collaborator, bot, external or unknown, as elsewhere; empty when not looked up


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
    answered: str = ""  # head at which an earlier run already replied that it no longer applies
    replies: list[Reply] = field(default_factory=list)
    unanswered: bool = False  # a person replied after our last message in the thread


_TITLE = re.compile(r"^\*\*(?P<title>.+?)\*\*(?: · (?P<badge>.*))?$")


def answered_marker(tag: str, head_sha: str) -> str:
    """Hidden record on the reply that closes a thread, so a later run does not reply again when the
    resolution itself was refused (a token without that permission) and the thread stayed open."""
    return f"<!-- {tag}:answered:{head_sha} -->"


def replied_marker(tag: str, head_sha: str) -> str:
    """Hidden record on the reply that answers a person in a thread that stays open."""
    return f"<!-- {tag}:replied:{head_sha} -->"


TrustFn = Callable[[str, bool], str]  # (login, is_bot) -> trust level


def _thread_replies(
    raws: list[dict[str, Any]], ours: str, pr_author: str, trust: TrustFn | None
) -> tuple[list[Reply], bool]:
    """The replies people left in one thread, oldest first, and whether the last word is theirs."""
    replies: list[Reply] = []
    unanswered = False
    for raw in sorted(raws, key=lambda r: int(r.get("id") or 0)):
        body = raw.get("body") or ""
        if ours in body:
            unanswered = False  # our own reply: an answer, or the note that closed the thread
            continue
        text = sanitize(body)
        if not text:
            continue
        user = raw.get("user") or {}
        login = user.get("login") or "unknown"
        is_bot = user.get("type") == "Bot" or login.endswith("[bot]")
        role = "author" if pr_author and login == pr_author else "reviewer"
        replies.append(Reply(login, text[:MAX_REPLY_CHARS], role, trust(login, is_bot) if trust else ""))
        unanswered = True
    return replies[-MAX_REPLIES:], unanswered


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


def open_findings(
    gh: GitHub, number: int, tag: str, pr_author: str = "", trust: TrustFn | None = None
) -> list[OpenFinding]:
    """This tool's inline comments on the pull request whose threads are still open, oldest first, each
    with what people replied in its thread. Raises GitHubError when the threads cannot be listed
    (without them nothing could be resolved)."""
    prefix = inline_marker(tag, "")[:-4]  # "<!-- tag:finding:"
    answered_prefix = answered_marker(tag, "")[:-4]  # "<!-- tag:answered:"
    ours = f"<!-- {tag}:"  # every comment and reply of ours carries a marker
    by_comment: dict[int, dict[str, Any]] = {}
    for t in gh.review_threads(number):
        for cid in t["comment_ids"]:
            if cid is not None:
                by_comment[int(cid)] = t
    comments = gh.review_comments(number)
    answered: dict[str, str] = {}  # thread id -> head of the reply that closed it
    for raw in comments:
        body = raw.get("body") or ""
        i = body.find(answered_prefix)
        thread = by_comment.get(int(raw.get("id") or 0))
        if i != -1 and thread is not None and thread["id"]:
            answered[str(thread["id"])] = body[i + len(answered_prefix) : body.find("-->", i)].strip()
    replies_by_root: dict[int, list[dict[str, Any]]] = {}
    for raw in comments:
        if raw.get("in_reply_to_id"):
            replies_by_root.setdefault(int(raw["in_reply_to_id"]), []).append(raw)
    out: list[OpenFinding] = []
    for raw in comments:
        body = raw.get("body") or ""
        if prefix not in body or raw.get("in_reply_to_id"):
            continue
        thread = by_comment.get(int(raw.get("id") or 0))
        if thread is None or thread["isResolved"] or not thread["id"]:
            continue
        title, text, severity, recommendation = parse_posted(body)
        if not title:
            continue
        replies, unanswered = _thread_replies(replies_by_root.get(int(raw["id"]), []), ours, pr_author, trust)
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
                answered=answered.get(str(thread["id"]), ""),
                replies=replies,
                unanswered=unanswered,
            )
        )
    return out


def as_claims(findings: list[OpenFinding], start: int = 1, old_head: str = "") -> list[Claim]:
    """The open, not yet answered comments as claims ``X{start}``, ``X{start+1}``, ... carrying their
    thread for later."""
    source = f"{EARLIER_REVIEW} of {old_head[:12]}" if old_head else EARLIER_REVIEW
    claims: list[Claim] = []
    for i, f in enumerate([f for f in findings if not f.answered][:MAX_CLAIMS]):
        extra: dict[str, Any] = {
            "title": f.title,
            "severity": f.severity,
            "thread_id": f.thread_id,
            "comment_id": f.comment_id,
            "url": f.url,
        }
        if f.recommendation:
            extra["recommendation"] = f.recommendation
        if f.replies:
            extra["replies"] = [asdict(r) for r in f.replies]
        if f.unanswered:
            extra["unanswered"] = True
        text = f"{f.title}: {f.text}" if f.text else f.title
        claims.append(Claim(f"X{start + i}", text, f.path, f.line, source, extra))
    return claims


def is_followup(claim: Claim) -> bool:
    return bool(claim.extra.get("thread_id"))


def confirming_finding(review: Review, claim: Claim) -> Finding | None:
    """The new finding that confirms an earlier comment: it cites the claim's id in its evidence, or sits
    in the same file under the same title."""
    key = (claim.file, normalize_title(claim.title))
    for f in review.findings:
        if claim.id in _CLAIM_ID.findall(f.evidence) or (f.file, normalize_title(f.title)) == key:
            return f
    return None


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


RESOLVE_HINT = (
    "GitHub requires `contents: write` on the token for resolveReviewThread, even though it changes no "
    'content; `pull-requests: write` alone is refused with "Resource not accessible by integration". Give '
    "the job that permission, or pass a token that has it as `github_token`. The replies stand, and a "
    "person can resolve the threads meanwhile."
)


def resolve_thread(gh: GitHub, thread_id: str, what: str, log=print) -> bool:
    """Resolve one thread; a refusal is a warning (the hint on the missing permission is logged once by the
    caller, after all its attempts)."""
    try:
        gh.resolve_thread(thread_id)
    except GitHubError as exc:
        log(f"::warning::could not resolve the thread of {what}: {exc}")
        return False
    return True


def retry_resolutions(gh: GitHub, findings: list[OpenFinding], log=print) -> int:
    """Resolve the threads an earlier run answered but could not resolve; returns how many it managed."""
    done = failed = 0
    for f in findings:
        if not f.answered:
            continue
        if resolve_thread(gh, f.thread_id, f"{f.path}:{f.line} {f.title}", log):
            done += 1
        else:
            failed += 1
    if done:
        log(f"resolved {done} thread(s) an earlier run had answered")
    if failed:
        log(f"::warning::{failed} thread(s) answered by an earlier run are still not resolved. {RESOLVE_HINT}")
    return done


def outcome_lines(review: Review, claims: list[Claim]) -> list[str]:
    """One line per earlier comment with what the run decided, for the log: the pull request only shows
    the replies, and a job's output should say on its own which comments are done with."""
    by_id = {c.id: c for c in claims if is_followup(c)}
    out = []
    for v in review.external_claims:
        c = by_id.get(v.id)
        if c is None:
            continue
        what = f"{v.id} ({c.location} {c.title[:70]})"
        reason = " ".join((v.reason or "").split())[:160]
        if v.verdict == "refuted":
            out.append(f"{what}: no longer applies, thread answered and resolved" + (f": {reason}" if reason else ""))
        elif v.verdict == "confirmed" and c.extra.get("unanswered"):
            out.append(f"{what}: still applies, the reply in its thread is answered, thread left open")
        elif v.verdict == "confirmed":
            out.append(f"{what}: still applies, thread left open")
        else:
            out.append(f"{what}: undecided, thread left open" + (f": {reason}" if reason else ""))
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


MAX_REPLY = 320  # characters: about two plain sentences, the length of the comment it answers

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`'\"(])")
_ENUMERATION = re.compile(r"\s*:?\s+(?=(?:1[.)]|\(1\))\s)")  # "...checked: 1. first 2. second" lists its cases
_PREAMBLE = re.compile(
    r"^\s*(?:because|since|as|so)\s+"
    r"|^\s*(?:this|the (?:issue|concern|claim|comment|problem))\s+(?:is|was|has been)\s+"
    r"(?:addressed|resolved|fixed)(?:\s+(?:in|at|by|on)\s+the\s+(?:head|current|latest|new)\s+commit)?\s*[:.,;]*\s*"
    r"|^\s*(?:at|in|by|on)\s+the\s+(?:head|current|latest|new)\s+commit\s*[:.,;]*\s*",
    re.I,
)


def reply_text(reason: str) -> str:
    """The verifier's reason as the answer posted in the thread: its first sentence or two, in the register
    of the comment it closes. The verdict preamble ("refuted because the issue was fixed at the head
    commit") goes; what is left says what in the code settles it."""
    text = " ".join(posted(reason).split())
    for _ in range(3):
        text = _PREAMBLE.sub("", text)
    text = _ENUMERATION.split(text, 1)[0].rstrip(":;, ")
    if not text:
        return ""
    sentences = _SENTENCE_END.split(text)
    out = sentences[0]
    if len(sentences) > 1 and len(out) + 1 + len(sentences[1]) <= MAX_REPLY:
        out += " " + sentences[1]
    return _sentence(out)


def reply_body(tag: str, head_sha: str, reason: str, *, still: bool = False) -> str:
    """The reply in the form of the comment it answers: a bold line with its badge, then the text. It closes
    the thread ("No longer applies"), or answers a person in a thread that stays open ("Still applies")."""
    if still:
        lines = [replied_marker(tag, head_sha), f"**Still applies** · at {head_sha[:12]}"]
    else:
        lines = [answered_marker(tag, head_sha), f"**No longer applies** · at {head_sha[:12]}"]
    text = reply_text(reason)
    if text:
        lines += ["", text]
    lines += ["", f"<sub>{ADVISORY_SHORT}</sub>"]
    return "\n".join(lines)


def pending_answers(review: Review, claims: list[Claim]) -> list[tuple[Claim, str]]:
    """(claim, text) for every earlier comment the run confirmed after a person replied in its thread: they
    asked, so the thread gets an answer and stays open. The text is the verifier's reason, else the
    confirming finding's own words; a comment with nothing new to say gets no reply."""
    by_id = {c.id: c for c in claims if is_followup(c)}
    out = []
    for v in review.external_claims:
        c = by_id.get(v.id)
        if c is None or v.verdict != "confirmed" or not c.extra.get("unanswered"):
            continue
        text = reply_text(v.reason)
        if not text:
            f = confirming_finding(review, c)
            text = finding_text(f) if f is not None else ""
        if text:
            out.append((c, text))
    return out


def resolve_addressed(
    gh: GitHub,
    number: int,
    head_sha: str,
    review: Review,
    claims: list[Claim],
    log=print,
    tag: str = "zephyr-reviewer",
) -> list[dict[str, Any]]:
    """Reply to and resolve the thread of every earlier comment the run refuted, and answer the person in
    the thread of every one it confirmed after they replied; returns what was resolved. A reply is short,
    in the form of the comment it answers, and is posted before the resolution so that it stands on its
    own: when the token may not resolve threads, the thread stays open with the answer in it, and a later
    run retries the resolution without replying again."""
    done: list[dict[str, Any]] = []
    failed = answered = 0
    for c, text in pending_answers(review, claims):
        try:
            gh.reply_to_review_comment(number, int(c.extra["comment_id"]), reply_body(tag, head_sha, text, still=True))
        except GitHubError as exc:
            log(f"::warning::could not reply to the thread of {c.id} ({c.location} {c.title}): {exc}")
            continue
        answered += 1
    if answered:
        log(
            f"answered {answered} reply(ies) in the thread(s) of earlier comment(s) that still apply at {head_sha[:12]}"
        )
    for c, reason in pending_resolutions(review, claims):
        reason = reply_text(reason)
        try:
            gh.reply_to_review_comment(number, int(c.extra["comment_id"]), reply_body(tag, head_sha, reason))
        except GitHubError as exc:
            log(f"::warning::could not reply to the thread of {c.id} ({c.location} {c.title}): {exc}")
            continue
        if not resolve_thread(gh, str(c.extra["thread_id"]), f"{c.id} ({c.location} {c.title})", log):
            failed += 1
            continue
        url = c.extra.get("url", "")
        done.append({"id": c.id, "file": c.file, "line": c.line, "title": c.title, "reason": reason, "url": url})
    if done:
        log(
            f"resolved {len(done)} earlier inline comment(s) that no longer apply at {head_sha[:12]}: "
            + "; ".join(f"{d['id']} {d['file']}:{d['line']} {d['title'][:60]}" for d in done)
        )
    if failed:
        log(f"::warning::{failed} thread(s) answered but not resolved. {RESOLVE_HINT}")
    return done
