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

A comment a person dismissed is different: its thread was resolved by someone other than this tool,
or its comment got a thumbs-down reaction. That is the project's decision on it. The next run is
told about it, and a new finding that restates it, in any wording, is not posted.
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
class Dismissed:
    """An inline comment of ours a person set aside: resolved the thread, or gave it a thumbs down."""

    comment_id: int
    thread_id: str
    path: str
    line: int
    title: str
    text: str
    by: str = ""  # who resolved it or reacted
    how: str = "resolved"  # "resolved" or "thumbs down"
    replies: list[Reply] = field(default_factory=list)
    url: str = ""
    open: bool = False  # the thread is still open (a thumbs down on an unresolved thread)


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


THUMBS_DOWN = "-1"  # GitHub's name for the 👎 reaction


@dataclass
class _Scan:
    """Everything one pass over the pull request's review comments yields."""

    by_comment: dict[int, dict[str, Any]]  # comment id -> its thread
    roots: list[dict[str, Any]]  # our root comments, oldest first
    answered: dict[str, str]  # thread id -> head of our reply that closed it
    replies_by_root: dict[int, list[dict[str, Any]]]
    thumbs_down: dict[int, str]  # root comment id -> login of a person who reacted 👎


def _scan(gh: GitHub, number: int, tag: str) -> _Scan:
    prefix = inline_marker(tag, "")[:-4]  # "<!-- tag:finding:"
    answered_prefix = answered_marker(tag, "")[:-4]  # "<!-- tag:answered:"
    by_comment: dict[int, dict[str, Any]] = {}
    for t in gh.review_threads(number):
        for cid in t["comment_ids"]:
            if cid is not None:
                by_comment[int(cid)] = t
    comments = gh.review_comments(number)
    answered: dict[str, str] = {}
    replies_by_root: dict[int, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for raw in comments:
        body = raw.get("body") or ""
        thread = by_comment.get(int(raw.get("id") or 0))
        i = body.find(answered_prefix)
        if i != -1 and thread is not None and thread["id"]:
            answered[str(thread["id"])] = body[i + len(answered_prefix) : body.find("-->", i)].strip()
        if raw.get("in_reply_to_id"):
            replies_by_root.setdefault(int(raw["in_reply_to_id"]), []).append(raw)
        elif prefix in body and thread is not None and thread["id"]:
            roots.append(raw)
    thumbs_down: dict[int, str] = {}
    for raw in roots:
        if by_comment[int(raw["id"])]["isResolved"]:
            continue  # already set aside; no need to ask about reactions
        try:
            reactions = gh.review_comment_reactions(int(raw["id"]))
        except GitHubError:
            continue  # a reaction we cannot read is a reaction we do not know about
        for r in reactions:
            user = r.get("user") or {}
            if r.get("content") == THUMBS_DOWN and user.get("type") != "Bot":
                thumbs_down[int(raw["id"])] = user.get("login") or "someone"
                break
    return _Scan(by_comment, roots, answered, replies_by_root, thumbs_down)


def _line_of(raw: dict[str, Any], thread: dict[str, Any]) -> int:
    return int(raw.get("line") or raw.get("original_line") or thread.get("line") or 0)


def open_findings(
    gh: GitHub, number: int, tag: str, pr_author: str = "", trust: TrustFn | None = None
) -> list[OpenFinding]:
    """This tool's inline comments on the pull request whose threads are still open and nobody gave a
    thumbs down, oldest first, each with what people replied in its thread. Raises GitHubError when the
    threads cannot be listed (without them nothing could be resolved)."""
    ours = f"<!-- {tag}:"  # every comment and reply of ours carries a marker
    scan = _scan(gh, number, tag)
    out: list[OpenFinding] = []
    for raw in scan.roots:
        cid = int(raw["id"])
        thread = scan.by_comment[cid]
        if thread["isResolved"] or cid in scan.thumbs_down:
            continue
        title, text, severity, recommendation = parse_posted(raw.get("body") or "")
        if not title:
            continue
        replies, unanswered = _thread_replies(scan.replies_by_root.get(cid, []), ours, pr_author, trust)
        out.append(
            OpenFinding(
                comment_id=cid,
                thread_id=str(thread["id"]),
                path=raw.get("path") or thread.get("path") or "",
                line=_line_of(raw, thread),
                title=title,
                text=text,
                severity=severity or "medium",
                recommendation=recommendation,
                outdated=raw.get("position") is None,
                url=raw.get("html_url") or "",
                answered=scan.answered.get(str(thread["id"]), ""),
                replies=replies,
                unanswered=unanswered,
            )
        )
    return out


def dismissed_findings(
    gh: GitHub, number: int, tag: str, pr_author: str = "", trust: TrustFn | None = None
) -> list[Dismissed]:
    """This tool's inline comments a person set aside: the thread was resolved by someone other than this
    tool (a thread this tool closed as no longer applying is not a dismissal), or the comment got a thumbs
    down while its thread is open. Oldest first, each with what people replied."""
    ours = f"<!-- {tag}:"
    scan = _scan(gh, number, tag)
    out: list[Dismissed] = []
    for raw in scan.roots:
        cid = int(raw["id"])
        thread = scan.by_comment[cid]
        tid = str(thread["id"])
        if thread["isResolved"]:
            if tid in scan.answered:
                continue  # closed by this tool: addressed, not dismissed
            by, how, is_open = thread.get("resolved_by") or "", "resolved", False
        elif cid in scan.thumbs_down:
            by, how, is_open = scan.thumbs_down[cid], "thumbs down", True
        else:
            continue
        title, text, _, _ = parse_posted(raw.get("body") or "")
        if not title:
            continue
        replies, _ = _thread_replies(scan.replies_by_root.get(cid, []), ours, pr_author, trust)
        out.append(
            Dismissed(
                comment_id=cid,
                thread_id=tid,
                path=raw.get("path") or thread.get("path") or "",
                line=_line_of(raw, thread),
                title=title,
                text=text,
                by=by,
                how=how,
                replies=replies,
                url=raw.get("html_url") or "",
                open=is_open,
            )
        )
    return out


MAX_DISMISSED = 40
NEAR_LINES = 3  # a new finding this close to a dismissed comment, in the same file, restates it


def dismissed_digest(dismissed: list[Dismissed]) -> str:
    """The block the models see: what was set aside, by whom, and what was said."""
    lines = []
    for d in dismissed[:MAX_DISMISSED]:
        loc = f"{d.path}:{d.line}" if d.line else d.path
        who = f"{d.how} by {d.by}" if d.by else d.how
        entry = f'- {loc} "{d.title}": {d.text[:400]} ({who})'
        for r in d.replies[-2:]:
            entry += f"\n  reply from {r.author}" + (f" ({r.role})" if r.role else "") + f": {r.body[:400]}"
        lines.append(entry)
    return "\n".join(lines)


MIN_TITLE_MATCH = 12  # characters: a title this short, contained in another, proves nothing


def _same_title(a: str, b: str) -> bool:
    """Equal titles, or one contained in the other ("Missing lock" in "Missing lock around the transfer")."""
    a, b = normalize_title(a), normalize_title(b)
    if not a or not b:
        return False
    short, long = sorted((a, b), key=len)
    return short == long or (len(short) >= MIN_TITLE_MATCH and short in long)


same_title = _same_title  # shared with previous.py: one notion of "the same point"


def _restates(f: Finding, d: Dismissed) -> bool:
    if f.file != d.path:
        return False
    if _same_title(f.title, d.title):
        return True
    if not f.line or not d.line:
        return False
    return (f.line - NEAR_LINES) <= d.line <= ((f.end_line or f.line) + NEAR_LINES)


def drop_dismissed(review: Review, dismissed: list[Dismissed], log=print) -> int:
    """Remove from the review every finding that restates a comment a person dismissed: same file and the
    same title (or one containing the other), or anchored within a few lines of it. Returns how many were
    removed, each one logged. The verdict follows what is left, see settle_verdict()."""
    kept: list[Finding] = []
    worst = review.highest_severity()
    dropped = 0
    for f in review.findings:
        match = next((d for d in dismissed if _restates(f, d)), None)
        if match is None:
            kept.append(f)
            continue
        dropped += 1
        who = f" by {match.by}" if match.by else ""
        log(
            f"not posted: {f.file}:{f.line} {f.title[:70]} restates an earlier comment {match.how}{who}"
            + (f" ({match.url})" if match.url else "")
        )
    review.findings = kept
    if dropped:
        settle_verdict(review, worst)
    return dropped


NOTHING_NEW = "Every finding of this run restates a comment that was dismissed earlier; nothing new is reported."


def settle_verdict(review: Review, worst_before: str) -> None:
    """Make the verdict and the summary agree with the findings left after a drop. Both were written with
    the dropped findings in view: a review asking for changes because of a finding that is no longer in it
    asks for nothing, and a summary naming that finding describes a review nobody sees."""
    if not review.findings:
        review.summary = NOTHING_NEW
        if review.verdict == "request_changes":
            review.verdict = "approve"
    elif (
        review.verdict == "request_changes"
        and worst_before in ("critical", "high")
        and review.highest_severity() in ("low", "suggestion")
    ):
        # What forced the verdict is gone and nothing left can block. A medium finding may be the one
        # the report called merge-blocking, so with one left the verdict stands.
        review.verdict = "comment"


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
_DONE = r"(?:addressed|resolved|fixed)"
_AT_HEAD = r"(?:in|at|by|on)\s+(?:the\s+)?(?:(?:head|current|latest|new)\s+commit|head\b)"
# What may follow a preamble: ", where" (the clause after it is the answer), punctuation, or the end. Without
# one of them the words are content ("This is fixed size", "At head of the queue"), and stay. Punctuation
# ends a preamble only before a space or the end: the dot in "In head.S" or "At head.next" is part of a name.
_THEN = r"(?:\s*,\s*where\s+|\s*[:.,;]+(?:\s+|$)|\s*$)"
_PREAMBLE = re.compile(
    r"^\s*(?:because|since|as|so)\s+"
    rf"|^\s*(?:this|the (?:issue|concern|claim|comment|problem))\s+(?:is|was|has been)\s+{_DONE}"
    rf"(?:\s+{_AT_HEAD})?{_THEN}"  # "This was fixed in the head commit, where ..."
    rf"|^\s*{_DONE}(?:\s+{_AT_HEAD}{_THEN}|\s*:\s*)"  # "Resolved at head: ...", "Fixed: ..."
    rf"|^\s*{_AT_HEAD}{_THEN}",
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
