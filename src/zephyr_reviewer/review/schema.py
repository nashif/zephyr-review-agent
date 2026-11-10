# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""The review the model must hand back, and its validation against the diff."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = ["critical", "high", "medium", "low", "suggestion"]
CATEGORIES = [
    "bug",
    "security",
    "concurrency",
    "build_config",
    "devicetree",
    "driver",
    "api",
    "error_handling",
    "portability",
    "tests",
    "documentation",
    "commit_message",
    "maintainability",
    "coding_guidelines",
]
VERDICTS = ["approve", "comment", "request_changes"]
PRIORITIES = ["must", "should", "optional"]
CONFIDENCES = ["high", "medium", "low"]

SUBMIT_TOOL_NAME = "submit_review"

SUBMIT_REVIEW_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SUBMIT_TOOL_NAME,
        "description": (
            "Submit the final review. Call exactly once, after investigating with the other tools. "
            "Calling it ends the review."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary",
                "verdict",
                "confidence",
                "findings",
                "test_assessment",
                "commit_message_assessment",
            ],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One or two sentences: the review result and the most important issue, if any. "
                    "Do not describe what the pull request does; the author and maintainers know.",
                },
                "verdict": {"type": "string", "enum": VERDICTS},
                "confidence": {
                    "type": "string",
                    "enum": CONFIDENCES,
                    "description": "How well the available context supported the review.",
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "file",
                            "line",
                            "severity",
                            "category",
                            "title",
                            "problem",
                            "trigger",
                            "impact",
                            "recommendation",
                            "confidence",
                        ],
                        "properties": {
                            "file": {"type": "string", "description": "Repository-relative path as in the diff."},
                            "line": {
                                "type": "integer",
                                "description": "Line on the new side of the diff that the "
                                "finding concerns; 0 when not anchored to one changed line.",
                            },
                            "end_line": {"type": "integer", "description": "Last line of the range, or 0."},
                            "severity": {"type": "string", "enum": SEVERITIES},
                            "category": {"type": "string", "enum": CATEGORIES},
                            "title": {"type": "string", "description": "One short sentence naming the problem."},
                            "problem": {
                                "type": "string",
                                "description": "One or two plain sentences a maintainer can act on. The comment is "
                                "anchored to the line, so do not repeat file names or line numbers; name a "
                                "function or symbol only when the sentence needs it.",
                            },
                            "trigger": {
                                "type": "string",
                                "description": "One short clause: the configuration, input, timing or path that "
                                "exposes it. Empty when the problem sentence already says so.",
                            },
                            "impact": {
                                "type": "string",
                                "description": "One short clause: what breaks. Empty when the problem sentence "
                                "already says so.",
                            },
                            "recommendation": {
                                "type": "string",
                                "description": "One sentence saying what to change; put code in suggested_code "
                                "rather than in prose.",
                            },
                            "suggested_code": {
                                "type": "string",
                                "description": "Drop-in replacement for the cited "
                                "lines, correctly indented, no diff markers; empty when unhelpful.",
                            },
                            "confidence": {"type": "string", "enum": CONFIDENCES},
                            "evidence": {
                                "type": "string",
                                "description": "Which lookups support this finding, "
                                "e.g. 'read_file drivers/i2c/i2c_foo.c:120-160; find_references foo_init'.",
                            },
                            "speculative": {
                                "type": "boolean",
                                "description": "True when the trigger path rests on an assumption you could not "
                                "verify. Reported anyway, with severity capped at medium.",
                            },
                        },
                    },
                },
                "test_assessment": {
                    "type": "string",
                    "description": "Only when the tests are missing or wrong: what material behavior is untested or "
                    "what a test gets wrong, and whether tests are required before merge. Empty string when the "
                    "tests are adequate or the change needs none. Never describe what is tested.",
                },
                "test_plan": {
                    "type": "array",
                    "description": "Only when a test plan was requested: the twister runs that would exercise this "
                    "change, most important first. At most 15 entries.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["suite", "platforms", "reason", "priority"],
                        "properties": {
                            "suite": {
                                "type": "string",
                                "description": "Suite directory, e.g. tests/drivers/i2c/i2c_api.",
                            },
                            "scenario": {
                                "type": "string",
                                "description": "One scenario id from that suite's "
                                "tests.yaml, or empty for the whole suite.",
                            },
                            "platforms": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Platform identifiers as list_platforms reports them.",
                            },
                            "reason": {"type": "string", "description": "What this run verifies about the change."},
                            "priority": {"type": "string", "enum": PRIORITIES},
                            "build_only": {"type": "boolean", "description": "True when building is enough."},
                        },
                    },
                },
                "external_claims": {
                    "type": "array",
                    "description": "One entry per claim X* in the opening message: confirmed (it is also in "
                    "findings), refuted (reason cites the disproving code) or undecided.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "verdict", "reason"],
                        "properties": {
                            "id": {"type": "string"},
                            "verdict": {"type": "string", "enum": ["confirmed", "refuted", "undecided"]},
                            "reason": {
                                "type": "string",
                                "description": "One or two plain sentences saying what settles it; posted as "
                                "the answer.",
                            },
                        },
                    },
                },
                "discussion_assessment": {
                    "type": "string",
                    "description": "Only when the existing discussion leaves a problem: a reviewer's request the head "
                    "commit does not address, or a claim in the discussion the code contradicts, with evidence. "
                    "Empty string otherwise. Never summarize the discussion or list what was resolved.",
                },
                "commit_message_assessment": {
                    "type": "string",
                    "description": "Only when a commit message has a problem: it does not explain why, does not match "
                    "the change, mixes unrelated changes, or lacks a required trailer. Empty string otherwise.",
                },
            },
        },
    },
}


@dataclass
class Finding:
    file: str
    line: int
    severity: str
    category: str
    title: str
    problem: str
    trigger: str = ""
    impact: str = ""
    recommendation: str = ""
    suggested_code: str = ""
    confidence: str = "medium"
    evidence: str = ""
    end_line: int = 0
    speculative: bool = False
    inline: bool = False  # anchored to a line GitHub will accept an inline comment on
    notes: list[str] = field(default_factory=list)


@dataclass
class ClaimVerdict:
    """What the review decided about a finding supplied from outside (a human's note, another tool)."""

    id: str
    verdict: str  # confirmed | refuted | undecided
    reason: str = ""
    title: str = ""
    file: str = ""
    line: int = 0
    source: str = ""
    finding_id: str = ""  # the verified finding it became, when confirmed in the specialist pipeline


@dataclass
class TestPlanEntry:
    suite: str
    scenario: str
    platforms: list[str]
    reason: str
    priority: str
    build_only: bool = False
    notes: list[str] = field(default_factory=list)  # validation outcomes, filled by review.testplan

    def twister(self) -> str:
        target = f"-s {self.suite}/{self.scenario}" if self.scenario else f"-T {self.suite}"
        plats = " ".join(f"-p {p}" for p in self.platforms)
        flags = " --build-only" if self.build_only else ""
        return f"west twister -v --inline-logs{flags} {target} {plats}".rstrip()


@dataclass
class Review:
    summary: str
    verdict: str
    confidence: str
    findings: list[Finding]
    test_assessment: str = ""
    commit_message_assessment: str = ""
    discussion_assessment: str = ""
    test_plan: list[TestPlanEntry] = field(default_factory=list)
    external_claims: list[ClaimVerdict] = field(default_factory=list)

    def highest_severity(self) -> str:
        for sev in SEVERITIES:
            if any(f.severity == sev for f in self.findings):
                return sev
        return "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Review:
        findings = [Finding(**f) for f in d.get("findings") or []]
        plan = [TestPlanEntry(**e) for e in d.get("test_plan") or []]
        claims = [ClaimVerdict(**c) for c in d.get("external_claims") or []]
        return cls(
            summary=d.get("summary") or "",
            verdict=d.get("verdict") or "comment",
            confidence=d.get("confidence") or "medium",
            findings=findings,
            test_assessment=d.get("test_assessment") or "",
            commit_message_assessment=d.get("commit_message_assessment") or "",
            discussion_assessment=d.get("discussion_assessment") or "",
            test_plan=plan,
            external_claims=claims,
        )


_ALL_CLEAR = re.compile(
    r"^(none|n/?a|no (issues?|problems?|concerns?|gaps?|changes? (needed|required))"
    r"|nothing (to (report|add|note)|further)"
    r"|not applicable|fine|ok|okay|adequate|sufficient|looks good|lgtm|good)\b"
    r"|\b(no (issues?|problems?|concerns?|gaps?) (found|identified|noted)"
    r"|are adequate|is adequate|are sufficient|is sufficient|as expected"
    r"|well covered|adequately covered|properly (tested|covered)"
    r"|all (threads|requests|comments) (are |have been )?(addressed|resolved))\b",
    re.I,
)
_PROBLEM_MARKERS = re.compile(
    r"\b(missing|lacks?|lacking|untested|not (tested|covered|exercised|addressed|verified)"
    r"|does not|doesn't|do not|don't|should|must|needs?|required|however|but"
    r"|wrong|incorrect|fails?|broken|contradicts?|unaddressed|still open|gap)\b",
    re.I,
)


def _issues_only(text: Any, max_clear_len: int = 240) -> str:
    """Drop short all-clear boilerplate ('tests are adequate'); keep anything that names a problem."""
    t = str(text or "").strip()
    if not t:
        return ""
    if len(t) <= max_clear_len and _ALL_CLEAR.search(t) and not _PROBLEM_MARKERS.search(t):
        return ""
    return t


def _norm_enum(value: Any, allowed: list[str], default: str) -> str:
    v = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if v in allowed:
        return v
    aliases = {
        "requestchanges": "request_changes",
        "changes_requested": "request_changes",
        "info": "suggestion",
        "nit": "suggestion",
        "minor": "low",
        "major": "high",
        "blocker": "critical",
    }
    return aliases.get(v, default)


def parse_review(args: dict[str, Any], changed_lines: dict[str, set[int]]) -> tuple[Review | None, list[str]]:
    """Validate ``submit_review`` arguments. Returns (review, errors); errors non-empty means reject and retry."""
    errors: list[str] = []
    if not isinstance(args, dict):
        return None, ["arguments must be a JSON object"]
    summary = str(args.get("summary") or "").strip()
    if not summary:
        errors.append("summary is required")
    verdict = _norm_enum(args.get("verdict"), VERDICTS, "")
    if not verdict:
        errors.append(f"verdict must be one of {VERDICTS}")
    confidence = _norm_enum(args.get("confidence"), CONFIDENCES, "medium")
    raw_findings = args.get("findings")
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        errors.append("findings must be an array")
        raw_findings = []

    findings: list[Finding] = []
    for i, rf in enumerate(raw_findings):
        if not isinstance(rf, dict):
            errors.append(f"findings[{i}] must be an object")
            continue
        title = str(rf.get("title") or "").strip()
        problem = str(rf.get("problem") or rf.get("description") or "").strip()
        if not title or not problem:
            errors.append(f"findings[{i}] needs title and problem")
            continue
        path = str(rf.get("file") or "").strip().lstrip("/")
        if path.startswith(("a/", "b/")) and path[2:] in changed_lines:
            path = path[2:]
        try:
            line = int(rf.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        try:
            end_line = int(rf.get("end_line") or 0)
        except (TypeError, ValueError):
            end_line = 0
        f = Finding(
            file=path,
            line=max(line, 0),
            end_line=max(end_line, 0),
            severity=_norm_enum(rf.get("severity"), SEVERITIES, "medium"),
            category=_norm_enum(rf.get("category"), CATEGORIES, "bug"),
            title=title,
            problem=problem,
            trigger=str(rf.get("trigger") or "").strip(),
            impact=str(rf.get("impact") or "").strip(),
            recommendation=str(rf.get("recommendation") or "").strip(),
            suggested_code=str(rf.get("suggested_code") or "").rstrip(),
            confidence=_norm_enum(rf.get("confidence"), CONFIDENCES, "medium"),
            evidence=str(rf.get("evidence") or "").strip(),
            speculative=bool(rf.get("speculative", False)),
        )
        if f.speculative and f.severity in ("critical", "high"):
            f.notes.append(f"speculative: severity capped at medium (was {f.severity})")
            f.severity = "medium"
        if path and path not in changed_lines:
            f.notes.append("file is not changed by this pull request; shown in the summary only")
            f.line, f.end_line = 0, 0
        elif f.line:
            lines = changed_lines.get(path, set())
            if f.line in lines and (not f.end_line or f.end_line >= f.line):
                f.inline = True
                if f.end_line and any(n not in lines for n in range(f.line, f.end_line + 1)):
                    f.end_line = 0  # range leaves the diff; anchor on the single line instead
            else:
                f.notes.append(f"line {f.line} is not on the new side of the diff; shown in the summary only")
        findings.append(f)

    plan: list[TestPlanEntry] = []
    raw_plan = args.get("test_plan") or []
    if not isinstance(raw_plan, list):
        errors.append("test_plan must be an array")
        raw_plan = []
    for i, rp in enumerate(raw_plan[:15]):
        if not isinstance(rp, dict):
            errors.append(f"test_plan[{i}] must be an object")
            continue
        suite = str(rp.get("suite") or "").strip().strip("/")
        if not suite:
            errors.append(f"test_plan[{i}] needs a suite directory")
            continue
        scenario = str(rp.get("scenario") or "").strip()
        if scenario.startswith(suite + "/"):
            scenario = scenario[len(suite) + 1 :]
        plats_raw = rp.get("platforms") or []
        if isinstance(plats_raw, str):
            plats_raw = plats_raw.replace(",", " ").split()
        plats = [str(x).strip() for x in plats_raw if str(x).strip()]
        plan.append(
            TestPlanEntry(
                suite=suite,
                scenario=scenario,
                platforms=list(dict.fromkeys(plats)),
                reason=str(rp.get("reason") or "").strip(),
                priority=_norm_enum(rp.get("priority"), PRIORITIES, "should"),
                build_only=bool(rp.get("build_only", False)),
            )
        )

    if errors:
        return None, errors
    review = Review(
        summary=summary,
        verdict=verdict,
        confidence=confidence,
        findings=findings,
        test_assessment=_issues_only(args.get("test_assessment")),
        commit_message_assessment=_issues_only(args.get("commit_message_assessment")),
        discussion_assessment=_issues_only(args.get("discussion_assessment")),
        test_plan=plan,
        external_claims=[
            ClaimVerdict(
                id=str(v.get("id") or "").strip(),
                verdict=_norm_enum(v.get("verdict"), ["confirmed", "refuted", "undecided"], "undecided"),
                reason=str(v.get("reason") or "").strip(),
            )
            for v in (args.get("external_claims") or [])
            if isinstance(v, dict) and str(v.get("id") or "").strip()
        ]
        if isinstance(args.get("external_claims"), list)
        else [],
    )
    return review, []
