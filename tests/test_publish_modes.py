# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Publish modes: post, ask (with confirmation and editing), never; and posting a saved review."""

from __future__ import annotations

import json
import os
import stat

import httpx

from tests.test_agent import GOOD_REVIEW
from zephyr_reviewer import cli
from zephyr_reviewer.review import publish as pub
from zephyr_reviewer.review.agent import RunResult
from zephyr_reviewer.review.config import ReviewConfig
from zephyr_reviewer.review.github_api import GitHub, PullRequest
from zephyr_reviewer.review.openrouter import Usage
from zephyr_reviewer.review.policy import Policy
from zephyr_reviewer.review.schema import Review, parse_review

CHANGED = {"drivers/i2c/i2c_foo.c": {17, 18, 19}, "drivers/i2c/Kconfig": {20}}


def _gh(seen: list) -> GitHub:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json={"html_url": "https://github.com/o/r/pull/7#pullrequestreview-1"})
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7#issuecomment-9"})

    return GitHub("tok", "o/r", client=httpx.Client(transport=httpx.MockTransport(handler)))


def _run() -> tuple[RunResult, PullRequest]:
    review, _ = parse_review(GOOD_REVIEW, CHANGED)
    pr = PullRequest(7, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    return RunResult(review, 2, Usage(), ["m"], False), pr


def test_policy_publish_only_tightens():
    floor = Policy(publish="post")
    assert floor.tighten(publish="ask")[0].publish == "ask"
    assert floor.tighten(publish="never")[0].dry_run
    asked = Policy(publish="ask")
    p, rejected = asked.tighten(publish="post")
    assert p.publish == "ask" and rejected == ["publish=post is looser than the operator floor ask"]
    assert asked.tighten(publish="never")[0].publish == "never"


def test_config_publish_modes(monkeypatch):
    monkeypatch.setenv("INPUT_PUBLISH", "ask")
    assert ReviewConfig.from_env().publish == "ask"
    monkeypatch.setenv("INPUT_DRY_RUN", "true")
    cfg = ReviewConfig.from_env()
    assert cfg.publish == "never" and cfg.dry_run
    monkeypatch.setenv("INPUT_DRY_RUN", "false")
    monkeypatch.setenv("INPUT_PUBLISH", "maybe")
    assert any("publish must be one of" in e for e in ReviewConfig.from_env().validate())


def test_never_and_ask_without_terminal_do_not_post():
    run, pr = _run()
    seen: list = []
    logs: list[str] = []
    out = pub.publish(ReviewConfig(publish="never"), _gh(seen), pr, run, lambda e: "body", log=logs.append)
    assert out["posted"] == "false" and seen == []
    out = pub.publish(
        ReviewConfig(publish="ask", repo_slug="o/r"), _gh(seen), pr, run, lambda e: "body", log=logs.append
    )
    assert out["posted"] == "false" and seen == []
    assert any("zephyr-reviewer post --output-dir review-output --repository o/r" in m for m in logs)


def test_ask_posts_only_when_confirmed_and_uses_the_edited_body():
    run, pr = _run()
    seen: list = []
    asked: list = []

    def decline(body, inline):
        asked.append((body, len(inline)))
        return False, body, inline

    cfg = ReviewConfig(publish="ask", post_inline=True)
    out = pub.publish(cfg, _gh(seen), pr, run, lambda e: "original", log=lambda m: None, confirm=decline)
    assert out["posted"] == "false" and seen == [] and asked == [("original", 2)]

    def accept_edited(body, inline):
        return True, body + "\nedited by a human", inline[:1]  # a human drops the second inline comment

    out = pub.publish(cfg, _gh(seen), pr, run, lambda e: "original", log=lambda m: None, confirm=accept_edited)
    assert out["posted"] == "true" and out["comment_url"].endswith("issuecomment-9")
    posted = [b for m, p, b in seen if m == "POST" and p.endswith("/issues/7/comments")][0]
    assert posted["body"] == "original\nedited by a human"
    review_req = [b for m, p, b in seen if p.endswith("/reviews")][0]
    assert len(review_req["comments"]) == 1 and review_req["commit_id"] == "h" * 40


def test_post_saved_review_round_trip(tmp_path):
    run, pr = _run()
    cfg = ReviewConfig(repo_slug="o/r", output_dir=str(tmp_path / "out"), comment_tag="zephyr-reviewer")
    from zephyr_reviewer.review.render import inline_comments

    pub.save_artifacts(
        cfg.output_dir, run, "<!-- zephyr-reviewer -->\nsaved body", pr, cfg, inline_comments(run.review)
    )
    record, body = pub.load_saved(cfg.output_dir)
    assert record["pr"] == 7 and record["repo"] == "o/r" and body.endswith("saved body")
    assert Review.from_dict(record["review"]).findings[0].inline  # inline anchoring survives the round trip

    # A human edits the comment, then posts with inline comments.
    (tmp_path / "out" / "comment.md").write_text("reviewed and trimmed by a maintainer", encoding="utf-8")
    seen: list = []
    out = pub.post_saved(cfg.output_dir, _gh(seen), inline=True, tag="zephyr-reviewer", log=lambda m: None)
    assert out["posted"] == "true"
    posted = [b for m, p, b in seen if m == "POST" and p.endswith("/issues/7/comments")][0]
    assert posted["body"] == "<!-- zephyr-reviewer -->\nreviewed and trimmed by a maintainer"  # marker restored
    assert len([b for m, p, b in seen if p.endswith("/reviews")][0]["comments"]) == 2

    declined = pub.post_saved(
        cfg.output_dir, _gh([]), inline=False, tag="t", log=lambda m: None, confirm=lambda b, i: (False, b, i)
    )
    assert declined == {"posted": "false"}


def test_interactive_confirm_answers_and_edit(tmp_path, monkeypatch, capsys):
    comment = tmp_path / "comment.md"
    comment.write_text("draft\n", encoding="utf-8")
    inline = [
        {
            "path": "a.c",
            "line": 19,
            "start_line": 17,
            "side": "RIGHT",
            "body": "<!-- t:finding:x -->\n**Lock leak**\nbody",
        },
        {"path": "b/Kconfig", "line": 20, "side": "RIGHT", "body": "<!-- t:finding:y -->\n**Bad select**"},
    ]
    (tmp_path / "inline_comments.json").write_text(json.dumps(inline), encoding="utf-8")
    pr = PullRequest(7, "", "", "", "", "", "", "h" * 40, "", False)
    confirm = cli._interactive_confirm(str(tmp_path), pr)

    prompts: list[str] = []
    answers = iter(["y"])

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    assert confirm("draft", inline) == (True, "draft", inline)
    assert prompts[-1].startswith("Post the summary comment and 2 inline comment(s) to PR #7?")
    assert "[i] edit inline_comments.json" in prompts[-1]
    shown = capsys.readouterr().err
    assert (
        "### 2 inline comment(s)" in shown and "--- [1] a.c:17-19 ---" in shown and "--- [2] b/Kconfig:20 ---" in shown
    )
    assert "**Lock leak**" in shown and "<!-- t:finding" not in shown  # markers are hidden in the preview

    answers = iter([""])
    assert confirm("draft", []) == (False, "draft", [])
    assert "[i]" not in prompts[-1]

    # 'e' edits comment.md; 'i' edits inline_comments.json (here: drop one); both re-read and ask again.
    editor = tmp_path / "ed.sh"
    editor.write_text(
        "#!/bin/sh\ncase \"$1\" in *.md) echo 'edited' >> \"$1\";; *.json) printf '%s' '"
        + json.dumps(inline[:1]).replace("'", "'\\''")
        + '\' > "$1";; esac\n',
        encoding="utf-8",
    )
    editor.chmod(editor.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    answers = iter(["e", "i", "y"])
    ok, body, kept = confirm("draft", inline)
    assert ok and body == "draft\nedited\n" and kept == inline[:1]
    assert prompts[-1].startswith("Post the summary comment and 1 inline comment(s) to PR #7?")

    answers = iter(["what", "n"])
    assert confirm("draft", inline) == (False, "draft", inline)


def test_post_command_requires_a_way_to_confirm(tmp_path, monkeypatch):
    run, pr = _run()
    cfg = ReviewConfig(repo_slug="o/r", output_dir=str(tmp_path / "out"))
    pub.save_artifacts(cfg.output_dir, run, "body", pr, cfg)
    monkeypatch.setattr(cli, "_has_terminal", lambda: False)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert cli.main(["post", "--output-dir", cfg.output_dir]) == 2  # no terminal, no --yes
    os.environ.pop("GITHUB_TOKEN")
    assert cli.main(["post", "--output-dir", cfg.output_dir, "--yes"]) == 2  # no token


def test_skip_reviewed_uses_the_summary_comment_and_its_head_marker():
    from zephyr_reviewer.review.render import head_marker, marker

    class FakeGitHub:
        def __init__(self, body):
            self.body = body
            self.calls = 0

        def find_comment(self, number, mark):
            self.calls += 1
            return (
                {"id": 5, "html_url": "https://github.com/o/r/pull/7#issuecomment-5", "body": self.body}
                if mark in self.body
                else None
            )

    _, pr = _run()
    same = FakeGitHub(marker("zephyr-reviewer") + "\n" + head_marker("zephyr-reviewer", pr.head_sha) + "\nold review")
    moved = FakeGitHub(marker("zephyr-reviewer") + "\n" + head_marker("zephyr-reviewer", "0" * 40) + "\nold review")
    none = FakeGitHub("unrelated comment")

    assert cli._skip_reason(ReviewConfig(), pr, same) is None and same.calls == 0  # never: not even looked up
    assert cli._skip_reason(ReviewConfig(skip_reviewed="any"), pr, same).startswith("already reviewed (https://")
    assert cli._skip_reason(ReviewConfig(skip_reviewed="any"), pr, moved).startswith("already reviewed (")
    assert cli._skip_reason(ReviewConfig(skip_reviewed="any"), pr, none) is None
    assert cli._skip_reason(ReviewConfig(skip_reviewed="same-head"), pr, same) == (
        f"already reviewed at this head {pr.head_sha[:12]} (https://github.com/o/r/pull/7#issuecomment-5)"
    )
    assert cli._skip_reason(ReviewConfig(skip_reviewed="same-head"), pr, moved) is None  # the PR moved on
    assert cli._skip_reason(ReviewConfig(skip_reviewed="same-head"), pr, None) is None  # no GitHub, nothing to check
    assert ReviewConfig(skip_reviewed="sometimes").validate()  # rejected by validation
    args = cli.build_parser().parse_args(["review", "--skip-reviewed"])
    assert args.skip_reviewed == "any"
    assert cli.build_parser().parse_args(["review", "--skip-reviewed", "same-head"]).skip_reviewed == "same-head"


def test_summary_comment_records_the_reviewed_head():
    from zephyr_reviewer.review.render import head_marker, render_comment

    run, pr = _run()
    body = render_comment(run.review, run, pr, repo="o/r", tag="zephyr-reviewer")
    assert body.splitlines()[1] == head_marker("zephyr-reviewer", pr.head_sha)


def test_a_failed_run_posts_nothing(tmp_path):
    """No comment, no review, no thread touched: the error goes to the log and the artifacts only."""
    seen: list = []
    logs: list[str] = []
    _, pr = _run()
    failed = RunResult(None, 0, Usage(), [], False, errors=["BrokenPipeError: server went away"])
    cfg = ReviewConfig(publish="post", output_dir=str(tmp_path / "out"))
    out = pub.publish(cfg, _gh(seen), pr, failed, lambda e: "## failure body", log=logs.append, claims=None)
    assert out["posted"] == "false" and out["verdict"] == "error" and seen == []
    assert "the review did not complete: nothing is posted" in logs
    # nor later from the saved artifacts
    pub.save_artifacts(cfg.output_dir, failed, "## failure body", pr, cfg, [], None)
    saved = pub.post_saved(cfg.output_dir, _gh(seen), inline=True, tag="zephyr-reviewer", log=logs.append)
    assert saved == {"posted": "false"} and seen == []
    assert any("did not complete (BrokenPipeError: server went away)" in m for m in logs)


DIFF_A = (
    "diff --git a/drivers/foo.c b/drivers/foo.c\n"
    "index 1111111..2222222 100644\n"
    "--- a/drivers/foo.c\n"
    "+++ b/drivers/foo.c\n"
    "@@ -10,3 +10,4 @@ static int foo(void)\n"
    " \tint ret;\n"
    "+\tret = bar();\n"
    " \treturn ret;\n"
)


def test_a_rebase_keeps_the_patch_digest_and_a_real_change_does_not():
    from types import SimpleNamespace as Commit

    from zephyr_reviewer.review.patch import patch_digest

    commits = [Commit(sha="a" * 40, subject="drivers: foo: call bar", body="Needed for baz.")]
    base = patch_digest(DIFF_A, commits)
    # The base moved: other blob ids, the hunk further down, new commit ids. The change is the same.
    rebased = DIFF_A.replace("1111111..2222222", "3333333..4444444").replace("-10,3 +10,4", "-42,3 +42,4")
    assert patch_digest(rebased, [Commit(sha="b" * 40, subject=commits[0].subject, body=commits[0].body)]) == base
    # A changed line, changed context, or a reworded commit message is a different change.
    assert patch_digest(DIFF_A.replace("ret = bar();", "ret = baz();"), commits) != base
    assert patch_digest(DIFF_A.replace(" \tint ret;", " \tint ret = 0;"), commits) != base
    assert patch_digest(DIFF_A, [Commit(sha="a" * 40, subject="drivers: foo: call bar", body="Fixes #1.")]) != base
    assert patch_digest(DIFF_A, []) != base


def test_same_patch_skips_a_moved_head_that_carries_the_reviewed_change(capsys):
    from zephyr_reviewer.review.render import head_marker, marker, patch_marker, render_comment

    class FakeGitHub:
        def __init__(self, body):
            self.body = body

        def find_comment(self, number, mark):
            return {"id": 5, "html_url": "https://github.com/o/r/pull/7#issuecomment-5", "body": self.body}

    run, pr = _run()
    tag = "zephyr-reviewer"
    # The summary comment records the digest of what it reviewed.
    body = render_comment(run.review, run, pr, repo="o/r", tag=tag, patch="abc123")
    assert patch_marker(tag, "abc123") in body and head_marker(tag, pr.head_sha) in body
    assert patch_marker(tag, "") not in render_comment(run.review, run, pr, repo="o/r", tag=tag)

    earlier = FakeGitHub(body.replace(pr.head_sha, "0" * 40))  # reviewed at another head
    cfg = ReviewConfig(skip_reviewed="same-patch")
    assert cli._skip_reason(cfg, pr, earlier) is None  # the head moved, so the patch decides
    reason = cli._same_patch(cfg, earlier, pr, "abc123")
    assert reason and "000000000000" in reason and "issuecomment-5" in reason
    assert cli._same_patch(cfg, earlier, pr, "def456") is None  # the change differs: review again
    assert "differs from what it reviewed" in capsys.readouterr().err
    # Only this mode compares patches; a comment from before the marker existed never matches.
    assert cli._same_patch(ReviewConfig(skip_reviewed="same-head"), earlier, pr, "abc123") is None
    old = FakeGitHub(marker(tag) + "\n" + head_marker(tag, "0" * 40))
    assert cli._same_patch(cfg, old, pr, "abc123") is None
    assert not any("skip_reviewed" in e for e in ReviewConfig(skip_reviewed="same-patch").validate())
    assert any("skip_reviewed" in e for e in ReviewConfig(skip_reviewed="sometimes").validate())
