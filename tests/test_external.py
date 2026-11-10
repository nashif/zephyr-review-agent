# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Findings supplied from outside the run become claims with ids, verdicts and a section in the comment."""

from __future__ import annotations

import json

from tests.test_agent import GOOD_REVIEW
from zephyr_reviewer.review.agent import RunResult
from zephyr_reviewer.review.external import Claim, claims_digest, parse_claims, reconcile
from zephyr_reviewer.review.github_api import PullRequest
from zephyr_reviewer.review.openrouter import Usage
from zephyr_reviewer.review.prompts import opening_message
from zephyr_reviewer.review.render import render_claims, render_comment
from zephyr_reviewer.review.schema import Review, parse_review

SAMPLE = """\
3 findings, most severe first:

1)
lib/utils/bitarray.c:226: The same two-line bounds condition is copy-pasted into three functions. More text.

Failure scenario: A future region function copies whichever neighbouring check is nearest.

2)
lib/utils/bitarray.c:659: The commit message states the other functions bound num_bits separately.

3)
An out-of-bounds write in sys_bitarray_xor is a bug fix that will be backported, but no Fixes #N line exists.
"""


def test_numbered_text_splits_into_claims_with_locations_and_titles():
    claims = parse_claims(SAMPLE, source="findings.txt")
    assert [c.id for c in claims] == ["X1", "X2", "X3"]
    assert (claims[0].file, claims[0].line) == ("lib/utils/bitarray.c", 226)
    assert claims[0].title == "The same two-line bounds condition is copy-pasted into three functions."
    assert "Failure scenario" in claims[0].text and "3 findings, most severe first" not in claims[0].text
    assert (claims[2].file, claims[2].line) == ("", 0)  # no path:line in the item
    assert claims[2].title.startswith("An out-of-bounds write in sys_bitarray_xor")
    digest = claims_digest(claims)
    assert digest.startswith("[X1] (lib/utils/bitarray.c:226) lib/utils/bitarray.c:226: The same")
    assert "[X3] An out-of-bounds" in digest


def test_bullets_paragraphs_and_json_are_accepted():
    bullets = parse_claims(
        "- drivers/foo.c:10: first thing\n- second thing in Kconfig:3\n- a word like Kconfig alone\n"
    )
    assert [(c.file, c.line) for c in bullets] == [("drivers/foo.c", 10), ("Kconfig", 3), ("", 0)]
    paras = parse_claims("one claim here.\n\nanother claim, no numbering.")
    assert [c.title for c in paras] == ["one claim here.", "another claim, no numbering."]
    single = parse_claims("just one line")
    assert len(single) == 1 and single[0].id == "X1"
    data = {
        "findings": [
            {"title": "Lock leak", "problem": "sem not given", "file": "a.c", "line": 7, "severity": "high"},
            {"description": "something in b.c:3"},
        ]
    }
    js = parse_claims(json.dumps(data), source="tool.json")
    assert js[0].title == "Lock leak" and js[0].file == "a.c" and js[0].line == 7
    assert js[0].as_finding()["severity"] == "high" and js[0].as_finding()["speculative"] is True
    assert js[1].file == "b.c" and js[1].line == 3 and js[1].as_finding()["severity"] == "medium"
    assert parse_claims("   ") == []


def test_opening_message_carries_claims_as_data_to_verify():
    pr = PullRequest(7, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    text = opening_message(pr, "ov", "diff", "", "o/r", claims="[X1] (a.c:3) a.c:3: leak", claims_source="notes.txt")
    assert "### Claims to verify (from notes.txt; leads to check" in text
    assert "<external_findings>\n[X1] (a.c:3) a.c:3: leak\n</external_findings>" in text
    assert "Every claim needs a verdict." in text
    assert "<external_findings>" not in opening_message(pr, "ov", "diff", "", "o/r")


def test_review_parses_claim_verdicts_and_reconcile_fills_the_gaps():
    args = {
        **GOOD_REVIEW,
        "external_claims": [
            {"id": "X1", "verdict": "confirmed", "reason": "see finding 1"},
            {"id": "X2", "verdict": "nonsense", "reason": "?"},
            {"id": "X9", "verdict": "refuted", "reason": "not a claim we gave"},
        ],
    }
    review, errors = parse_review(args, {"drivers/i2c/i2c_foo.c": {17, 18, 19}})
    assert review is not None and errors == []
    assert [(c.id, c.verdict) for c in review.external_claims] == [
        ("X1", "confirmed"),
        ("X2", "undecided"),
        ("X9", "refuted"),
    ]
    claims = [Claim("X1", "a.c:3: leak", "a.c", 3, "notes.txt"), Claim("X2", "b", source="notes.txt"), Claim("X3", "c")]
    reconcile(review, claims, [{"id": c.id, "verdict": c.verdict, "reason": c.reason} for c in review.external_claims])
    assert [(c.id, c.verdict, c.title) for c in review.external_claims] == [
        ("X1", "confirmed", "leak"),
        ("X2", "undecided", "b"),
        ("X3", "undecided", "c"),  # the model never mentioned it
    ]
    assert Review.from_dict(review.to_dict()).external_claims == review.external_claims


def test_comment_answers_every_claim():
    review, _ = parse_review(GOOD_REVIEW, {"drivers/i2c/i2c_foo.c": {17, 18, 19}})
    reconcile(
        review,
        [Claim("X1", "a.c:3: leak", "a.c", 3, "notes.txt"), Claim("X2", "no bug here", source="notes.txt")],
        [
            {"id": "X1", "verdict": "confirmed", "reason": ""},
            {"id": "X2", "verdict": "refuted", "reason": "the check exists."},
        ],
    )
    lines = render_claims(review)
    assert lines[0] == "### Claims checked (2 from notes.txt)"
    assert "1 confirmed, 1 refuted, 0 undecided. Confirmed claims appear among the findings." in lines[2]
    assert "- **confirmed** `a.c:3` leak" in lines and "- **refuted** no bug here: the check exists" in lines
    assert not any(" X1" in ln or " X2" in ln for ln in lines)  # the run's ids mean nothing to the reader
    pr = PullRequest(7, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    body = render_comment(review, RunResult(review, 1, Usage(), ["m"], False), pr, repo="o/r", tag="t")
    assert "### Claims checked" in body
    review.external_claims = []
    assert "Claims checked" not in render_comment(
        review, RunResult(review, 1, Usage(), ["m"], False), pr, repo="o/r", tag="t"
    )
