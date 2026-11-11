# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Markdown for the summary comment and the inline review comments."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .agent import RunResult
from .external import from_earlier_review
from .github_api import PullRequest, file_url
from .schema import SEVERITIES, ClaimVerdict, Finding, Review

SEVERITY_BADGE = {
    "critical": "🔴 critical",
    "high": "🟠 high",
    "medium": "🟡 medium",
    "low": "🔵 low",
    "suggestion": "⚪ suggestion",
}
VERDICT_LINE = {
    "approve": "🔎 **AI review**: No findings reported.",
    "comment": "💬 **Comment**: worth a look, nothing blocking.",
    "request_changes": "🛑 **Request changes**: at least one issue should be fixed before merge.",
}
_LANG = {
    "c": "c",
    "h": "c",
    "S": "asm",
    "py": "python",
    "cmake": "cmake",
    "txt": "cmake",
    "yaml": "yaml",
    "yml": "yaml",
    "dts": "dts",
    "dtsi": "dts",
    "overlay": "dts",
    "rst": "rst",
    "conf": "kconfig",
    "sh": "bash",
}


def marker(tag: str) -> str:
    return f"<!-- {tag} -->"


def head_marker(tag: str, head_sha: str) -> str:
    """Hidden record of the commit a summary comment reviewed, so a later run can tell whether the PR moved."""
    return f"<!-- {tag}:head:{head_sha} -->"


def _lang(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if base.startswith("Kconfig") or base.endswith("defconfig"):
        return "kconfig"
    return _LANG.get(base.rsplit(".", 1)[-1], "") if "." in base else ""


def _location(f: Finding, repo: str, sha: str, api_url: str) -> str:
    if not f.file:
        return "_general_"
    label = f"`{f.file}`" if not f.line else f"`{f.file}:{f.line}`" + (f"-{f.end_line}" if f.end_line else "")
    if not repo:
        return label
    return f"[{label}]({file_url(repo, sha, f.file, f.line or None, api_url)})"


# Ids the pipeline uses between its stages: specialist concerns C*, dismissals D*, hard cases H*, findings
# F*/HF*, external claims X*. They mean nothing to a reader of the pull request.
_ID = r"(?:[XCDH]\d{1,3}|HF\d{1,3}|F\d{1,3})"
_IDS = rf"{_ID}(?:\s*(?:,|/|&|and)\s*{_ID})*"
_NOUNS = r"(?:claims?|concerns?|candidates?|dismissals?|hard cases?|cases?|findings?|ids?)"
_VERDICT_PREFIX = re.compile(
    rf"^\s*(?:(?:the\s+)?{_NOUNS}\s+)?{_IDS}\s+(?:is|was|are|were)\s+"
    r"(?:refuted|confirmed|dismissed|not confirmed|unconfirmed|a false positive|invalid|valid|correct|incorrect)"
    r"\s*[:.,;-]*\s*",
    re.I,
)
_LEADING_VERDICT = re.compile(r"^\s*(?:refuted|confirmed|dismissed)\s*[:.-]\s*", re.I)
_PAREN_IDS = re.compile(rf"\s*[(\[]\s*(?:see\s+|cf\.?\s+|ids?\s+|from\s+)?{_IDS}\s*[)\]]")
_NOUN_IDS = re.compile(rf"\b({_NOUNS})\s+{_IDS}\b", re.I)
_REF_IDS = re.compile(rf"\b(in|by|of|from|see|per|under|with|as)\s+{_IDS}\b", re.I)


def scrub_ids(text: str) -> str:
    """Posted text without the run's internal ids: a leading "Claim X8 is refuted:" goes, "(C3, D1)" goes,
    "concern C3" becomes "concern". Bare tokens are left alone, since C1 or D2 can name a part on a board."""
    t = text or ""
    t = _VERDICT_PREFIX.sub("", t)
    t = _LEADING_VERDICT.sub("", t)
    t = _PAREN_IDS.sub("", t)
    t = _NOUN_IDS.sub(r"\1", t)
    t = _REF_IDS.sub(r"\1 the earlier analysis", t)
    return t


_CODE_SPAN = re.compile(r"(`+)[^`]*?\1|```.*?```", re.S)
_MENTION = re.compile(r"(?<![\w`/.])@([A-Za-z0-9][\w-]*)")


def guard_mentions(text: str) -> str:
    """Wrap every ``@word`` outside code in backticks. Doxygen commands such as ``@since`` or ``@param``,
    and names copied from the discussion, would otherwise notify whoever owns that username; the
    reviewer never mentions anyone."""
    out: list[str] = []
    pos = 0
    for m in _CODE_SPAN.finditer(text or ""):
        out.append(_MENTION.sub(r"`@\1`", text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_MENTION.sub(r"`@\1`", (text or "")[pos:]))
    return "".join(out)


def posted(text: str) -> str:
    """Text as it may reach the pull request: no internal ids, no mentions."""
    return guard_mentions(scrub_ids(text))


def _sentence(text: str) -> str:
    text = " ".join(posted(text).split())
    if not text:
        return ""
    first = text.split()[0].rstrip(",.;:")
    if first.isalpha() and first.islower():  # a plain word, not an identifier such as foo_transfer or k_sem_give
        text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def finding_text(f: Finding) -> str:
    """The finding's body as posted: the problem, with the trigger and impact folded in as clauses when
    they add something, and nothing else. Short enough to skim, specific enough to act on."""
    problem = _sentence(f.problem)
    extras = []
    low = problem.lower()
    if f.trigger and f.trigger.lower().rstrip(".") not in low:
        t = f.trigger.strip().rstrip(".")
        extras.append(
            t
            if t[:1].isupper() and t.split()[0].lower() in ("when", "if", "on", "only")
            else f"Happens when {t[:1].lower() + t[1:]}"
        )
    if f.impact and f.impact.lower().rstrip(".") not in low:
        extras.append(f.impact.strip().rstrip("."))
    return " ".join([problem, *(_sentence(e) for e in extras)]).strip()


def _finding_md(f: Finding, idx: int, repo: str, sha: str, api_url: str) -> str:
    badge = f"{SEVERITY_BADGE.get(f.severity, f.severity)}{' (speculative)' if f.speculative else ''}"
    title = posted(f.title)
    lines = [f"#### {idx}. {title}", "", f"{badge} · {_location(f, repo, sha, api_url)}", "", finding_text(f), ""]
    if f.recommendation:
        lines += [f"**Fix:** {_sentence(f.recommendation)}", ""]
    if f.suggested_code.strip():
        lines += [f"```{_lang(f.file)}", f.suggested_code.rstrip(), "```", ""]
    return "\n".join(lines)


def render_comment(
    review: Review,
    run: RunResult,
    pr: PullRequest,
    *,
    repo: str,
    tag: str,
    api_url: str = "https://api.github.com",
    title: str = "AI review (advisory)",
    details: bool = False,
    exclude_inline: bool = False,
    refuted: bool = False,
) -> str:
    """The top-level comment. With ``exclude_inline`` the anchored findings are left to inline comments
    and only counted here; unanchored findings always appear in full."""
    inline = [f for f in review.findings if f.inline] if exclude_inline else []
    listed = [f for f in review.findings if f not in inline]
    out = [
        marker(tag),
        head_marker(tag, pr.head_sha),
        f"## 🤖 {title}",
        "",
        VERDICT_LINE.get(review.verdict, review.verdict),
        "",
        f"**Findings:** {len(review.findings)} · **Highest severity:** {review.highest_severity()} · "
        f"**Confidence:** {review.confidence}",
        "",
        posted(review.summary),
        "",
    ]
    if inline:
        counts = ", ".join(
            f"{n} {sev}" for sev, n in ((s, sum(1 for f in inline if f.severity == s)) for s in SEVERITIES) if n
        )
        out += [f"{len(inline)} finding(s) are posted as inline comments on the changed lines ({counts}).", ""]
    earlier = [c for c in review.external_claims if from_earlier_review(c.source)]
    if earlier:
        out += [earlier_comments_line(earlier), ""]
    by_sev: dict[str, list[Finding]] = {s: [] for s in SEVERITIES}
    for f in listed:
        by_sev.setdefault(f.severity, []).append(f)
    idx = 0
    if inline and listed:
        out += ["### Findings not anchored to a changed line", ""]
    for sev in SEVERITIES:
        items = by_sev.get(sev) or []
        if not items:
            continue
        open_attr = " open" if sev in ("critical", "high", "medium") else ""
        out.append(f"<details{open_attr}><summary><b>{SEVERITY_BADGE[sev]}</b>: {len(items)} finding(s)</summary>\n")
        for f in items:
            idx += 1
            out.append(_finding_md(f, idx, repo, pr.head_sha, api_url))
        out.append("</details>\n")
    if review.test_assessment:
        out += ["### Test gaps", "", posted(review.test_assessment), ""]
    if review.commit_message_assessment:
        out += ["### Commit message issues", "", posted(review.commit_message_assessment), ""]
    if review.discussion_assessment:
        out += ["### Unresolved discussion", "", posted(review.discussion_assessment), ""]
    if review.test_plan:
        out += render_test_plan(review)
    if review.external_claims:
        out += render_claims(review)
    if refuted:
        out += render_refuted(run)
    out += ["---", f"<sub>{run_details(run, pr) + ' ' if details else ''}{ADVISORY}</sub>"]
    return "\n".join(out)


ADVISORY_SHORT = "AI review, advisory; verify before acting on it."

ADVISORY = (
    "This review is generated by a language model with tool access to the source tree. "
    "It is advisory; verify before acting on it."
)


def run_progress(run: RunResult) -> str:
    """What a run did, without models, tokens or cost: safe for logs and summaries by default."""
    return (
        f"review finished: {run.steps} step(s), {run.tool_call_count} tool call(s), {run.seconds:.0f}s"
        f"{' · budget exhausted, review forced' if run.forced else ''}"
    )


def run_details(run: RunResult, pr: PullRequest) -> str:
    """Models, steps, tool calls, tokens, cost and time of the run. Shown only when show_run_details is set."""
    cost = f" · ${run.usage.cost:.3f}" if run.usage.cost else ""
    if len(run.stages) > 1:
        how = " then ".join(
            f"{st['stage']}: {', '.join(st['models']) or 'unknown'} ({st['steps']} step(s))" for st in run.stages
        )
        how = f"in two stages, {how},"
    else:
        how = f"with {', '.join(run.models_used) or 'unknown model'}"
    return (
        f"Reviewed `{pr.head_sha[:12]}` {how} via OpenRouter · "
        f"{run.steps} step(s), {run.tool_call_count} tool call(s), {run.usage.total_tokens:,} tokens{cost} · "
        f"{run.seconds:.0f}s{' · budget exhausted, review forced' if run.forced else ''}."
    )


def render_failure(run: RunResult, pr: PullRequest, tag: str) -> str:
    return "\n".join(
        [
            marker(tag),
            "## 🤖 AI review (advisory)",
            "",
            f"The review of `{pr.head_sha[:12]}` did not complete: "
            + guard_mentions("; ".join(run.errors or ["unknown error"])),
            "",
            f"<sub>{run.steps} step(s), {run.usage.total_tokens:,} tokens.</sub>",
        ]
    )


def normalize_title(title: str) -> str:
    """A title reduced to what identifies the finding: no markdown, badge, case or punctuation differences."""
    t = title.strip()
    t = t.split(" · ", 1)[0]  # drop a trailing severity badge from a rendered comment line
    t = re.sub(r"[*`_]", "", t)
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return " ".join(t.split())


def finding_key(f: Finding) -> str:
    """Stable id of a finding, used to avoid re-posting it on a later run. Keyed on file and title only:
    line ranges drift between runs (the model picks the anchor) and between pushes (the code moves)."""
    return hashlib.sha1(f"{f.file}:{normalize_title(f.title)}".encode()).hexdigest()[:12]


def posted_title(body: str) -> str:
    """The normalized title of an inline comment this tool posted (second line, '**Title** · badge')."""
    lines = [ln for ln in body.splitlines() if ln.strip()]
    for ln in lines[:3]:
        if ln.startswith("**"):
            return normalize_title(ln)
    return ""


def inline_marker(tag: str, key: str) -> str:
    return f"<!-- {tag}:finding:{key} -->"


def inline_comments(review: Review, tag: str = "zephyr-reviewer") -> list[dict[str, Any]]:
    """One review comment per anchored finding, self-contained and short, carrying a hidden marker for dedup."""
    comments = []
    for f in review.findings:
        if not f.inline:
            continue
        badge = f"{SEVERITY_BADGE.get(f.severity, f.severity)}{' (speculative)' if f.speculative else ''}"
        body = [inline_marker(tag, finding_key(f)), f"**{posted(f.title)}** · {badge}", "", finding_text(f)]
        if f.recommendation:
            body += ["", f"**Fix:** {_sentence(f.recommendation)}"]
        if f.suggested_code.strip():
            body += ["", "```suggestion", f.suggested_code.rstrip(), "```"]
        body += ["", f"<sub>{ADVISORY_SHORT}</sub>"]
        c: dict[str, Any] = {"path": f.file, "line": f.end_line or f.line, "side": "RIGHT", "body": "\n".join(body)}
        if f.end_line and f.end_line > f.line:
            c["start_line"] = f.line
            c["start_side"] = "RIGHT"
        comments.append(c)
    return comments


PRIORITY_BADGE = {"must": "🔴 must", "should": "🟡 should", "optional": "⚪ optional"}


def render_test_plan(review: Review) -> list[str]:
    from .testplan import commands

    out = ["### Test plan", "", "| Priority | Test | Platforms | Verifies |", "| --- | --- | --- | --- |"]
    for e in review.test_plan:
        test = f"`{e.suite}`" + (f"<br>`{e.scenario}`" if e.scenario else "")
        if e.build_only:
            test += "<br><sub>build only</sub>"
        plats = ", ".join(f"`{p}`" for p in e.platforms) or "_suite defaults_"
        reason = posted(e.reason).replace("|", "\\|")
        if e.notes:
            reason += " <sub>⚠ " + "; ".join(e.notes).replace("|", "\\|") + "</sub>"
        out.append(f"| {PRIORITY_BADGE.get(e.priority, e.priority)} | {test} | {plats} | {reason} |")
    cmds = commands(review.test_plan)
    if cmds:
        out += ["", "<details><summary>twister commands</summary>", "", "```sh", *cmds, "```", "</details>"]
    out.append("")
    return out


def preview_inline(inline: list[dict[str, Any]]) -> str:
    """Human-readable listing of inline comments, as they would appear on the pull request."""
    if not inline:
        return ""
    out = [f"### {len(inline)} inline comment(s)"]
    for i, c in enumerate(inline, 1):
        where = f"{c['path']}:{c.get('start_line') or c['line']}" + (
            f"-{c['line']}" if c.get("start_line") and c.get("start_line") != c["line"] else ""
        )
        body = "\n".join(ln for ln in c["body"].splitlines() if not ln.startswith("<!-- "))
        out += ["", f"--- [{i}] {where} ---", body]
    return "\n".join(out)


def refuted_candidates(run: RunResult) -> list[tuple[str, str, str]]:
    """(title, stage, reason) of specialist concerns that verification or a hard case refuted."""
    rm = run.review_map or {}
    out = []
    for c in rm.get("concerns", []):
        if c.get("fate") == "refuted":
            out.append((str(c.get("title") or ""), str(c.get("stage") or ""), str(c.get("reason") or "").strip()))
    return out


def earlier_comments_line(earlier: list[ClaimVerdict]) -> str:
    """One line on the earlier run's inline comments that were re-checked: each is answered in its own
    thread (a reply and resolution when it no longer applies), so the summary only counts them."""
    tally = {v: sum(1 for c in earlier if c.verdict == v) for v in ("refuted", "confirmed", "undecided")}
    parts = []
    if tally["refuted"]:
        parts.append(f"{tally['refuted']} resolved as no longer applying")
    if tally["confirmed"]:
        parts.append(f"{tally['confirmed']} still open")
    if tally["undecided"]:
        parts.append(f"{tally['undecided']} left open, undecided")
    return f"Earlier inline comments re-checked at this head: {len(earlier)} ({', '.join(parts)})."


def render_claims(review: Review) -> list[str]:
    """What became of the findings supplied from outside the run: confirmed ones are among the findings above,
    the rest are listed with the reason, so the person who supplied them gets an answer to each. Claims made
    from the earlier run's own inline comments are answered in their threads, not here."""
    claims = [c for c in review.external_claims if not from_earlier_review(c.source)]
    if not claims:
        return []
    src = {c.source for c in claims if c.source}
    where = f" from {', '.join(sorted(src))}" if src else ""
    tally = {v: sum(1 for c in claims if c.verdict == v) for v in ("confirmed", "refuted", "undecided")}
    out = [
        f"### Claims checked ({len(claims)}{where})",
        "",
        f"{tally['confirmed']} confirmed, {tally['refuted']} refuted, {tally['undecided']} undecided. "
        "Confirmed claims appear among the findings.",
        "",
    ]
    for c in claims:
        loc = f"`{c.file}:{c.line}` " if c.file and c.line else f"`{c.file}` " if c.file else ""
        reason = posted(c.reason).strip()
        why = f": {reason[:300].rstrip('.')}" if reason and c.verdict != "confirmed" else ""
        out.append(f"- **{c.verdict}** {loc}{posted(c.title)}{why}")
    out.append("")
    return out


def render_refuted(run: RunResult) -> list[str]:
    items = refuted_candidates(run)
    if not items:
        return []
    out = [
        f"<details><summary>Also considered and refuted: {len(items)} candidate(s)</summary>",
        "",
        "These were raised by a specialist pass and rejected on verification. Listed for transparency; "
        "they are not findings.",
        "",
    ]
    for title, stage, reason in items:
        reason = posted(reason).strip()
        why = f": {reason[:240].rstrip('.')}" if reason else ""
        out.append(f"- {posted(title)} ({stage}){why}")
    out += ["", "</details>", ""]
    return out
