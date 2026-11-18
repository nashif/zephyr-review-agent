# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Re-reviews that build on the earlier run: what changed since, findings carried over, verdicts reused."""

from __future__ import annotations

import io
import json
import re
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace as Commit

import httpx
import pytest

from tests.test_agent import GOOD_REVIEW
from zephyr_reviewer import cli
from zephyr_reviewer.review.config import ReviewConfig
from zephyr_reviewer.review.external import EARLIER_SUMMARY
from zephyr_reviewer.review.github_api import GitHub, PullRequest
from zephyr_reviewer.review.patch import delta_since, describe_change
from zephyr_reviewer.review.previous import (
    MAX_REFUTED,
    carried_claims,
    fetch_run,
    matching_refutation,
    parse_record,
    refuted_digest,
    settle,
    summary_only,
)
from zephyr_reviewer.review.prompts import opening_message, scope_section
from zephyr_reviewer.review.render import finding_key, inline_marker, render_comment, run_marker
from zephyr_reviewer.review.schema import parse_review

TAG = "zephyr-reviewer"


def _file(path: str, *hunks: tuple[int, list[str]], index: str = "1111111..2222222") -> str:
    out = [f"diff --git a/{path} b/{path}", f"index {index} 100644", f"--- a/{path}", f"+++ b/{path}"]
    for start, lines in hunks:
        out += [f"@@ -{start},3 +{start},4 @@ ctx", *lines]
    return "\n".join(out) + "\n"


LOCK = [" \tk_sem_take(&lock);", "+\tret = xfer();", " \tk_sem_give(&lock);"]
INIT = [" \tint ret;", "+\tret = init();", " \treturn ret;"]
MSG = [Commit(subject="drivers: foo: add xfer", body="Body.")]


def _earlier() -> dict:
    diff = (
        _file("drivers/foo.c", (10, LOCK), (40, INIT))
        + _file("drivers/Kconfig", (5, [" config FOO", "+\tselect BAR", " \tbool"]))
        + _file("doc/gone.rst", (1, [" Title", "+text", " ====="]))
    )
    return describe_change(diff, MSG)


def test_delta_is_the_hunks_the_earlier_review_did_not_see():
    fixed = [" \tint ret;", "+\tret = init();", "+\tif (ret < 0) {", "+\t\treturn ret;", "+\t}", " \treturn ret;"]
    now = (
        # Rebased: other blob ids and positions. The first hunk is as reviewed, the second was reworked.
        _file("drivers/foo.c", (110, LOCK), (140, fixed), index="3333333..4444444")
        + _file("drivers/Kconfig", (9, [" config FOO", "+\tselect BAR", " \tbool"]), index="5555555..6666666")
        + _file("tests/new.c", (1, [" a", "+b", " c"]))
    )
    d = delta_since(_earlier(), now, MSG)
    assert d.files == ["drivers/foo.c", "tests/new.c"]
    assert d.unchanged == ["drivers/Kconfig"] and d.dropped == ["doc/gone.rst"]
    assert not d.messages_changed and not d.empty
    assert "+\tif (ret < 0) {" in d.diff_text and "+b" in d.diff_text
    assert "xfer()" not in d.diff_text and "select BAR" not in d.diff_text  # reviewed before
    assert "diff --git a/drivers/foo.c" in d.diff_text and "@@ -140,3 +140,4 @@" in d.diff_text
    assert (d.lines, d.total_lines) == (5, 7) and round(d.share, 2) == 0.71

    same = delta_since(describe_change(now, MSG), now, [Commit(subject="drivers: foo: add xfer", body="Reworded.")])
    assert same.files == [] and same.messages_changed and not same.empty and same.share == 0.0
    assert delta_since(describe_change(now, MSG), now, MSG).empty


def test_a_removed_hunk_marks_its_file_as_changed_without_anything_to_read():
    now = _file("drivers/foo.c", (10, LOCK)) + _file(
        "drivers/Kconfig", (5, [" config FOO", "+\tselect BAR", " \tbool"])
    )
    d = delta_since(_earlier(), now, MSG)
    assert d.files == ["drivers/foo.c"] and d.diff_text == "" and d.lines == 0
    assert d.unchanged == ["drivers/Kconfig"]


def _record(**over) -> dict:
    review, _ = parse_review(GOOD_REVIEW, {"drivers/i2c/i2c_foo.c": {17, 18, 19}, "drivers/i2c/Kconfig": {20}})
    rec = {"pr": 7, "head_sha": "a" * 40, "review": review.to_dict(), "change": _earlier()}
    rec.update(over)
    return rec


def _map() -> dict:
    return {
        "concerns": [
            {"id": "C1", "title": "Lock is not released", "file": "drivers/foo.c", "line": 12, "fate": "refuted",
             "by": "H1", "reason": "The caller releases it."},
            {"id": "C2", "title": "Kept", "file": "drivers/foo.c", "line": 3, "fate": "confirmed", "by": "V"},
            {"id": "C3", "title": "select of BAR is undefined", "file": "drivers/Kconfig", "line": 6,
             "fate": "refuted", "by": "H2", "reason": "BAR is defined in drivers/Kconfig.bar."},
        ],
        "refuted_earlier": [
            {"file": "doc/gone.rst", "line": 1, "title": "Title underline too short", "reason": "It matches.",
             "head": "9" * 40},
            {"file": "drivers/foo.c", "line": 12, "title": "lock is not released!", "reason": "dup", "head": "9" * 40},
        ],
    }  # fmt: skip


def test_the_record_gives_what_was_refuted_then_and_before():
    prev = parse_record(_record(), _map(), origin="test")
    assert prev is not None and prev.head_sha == "a" * 40 and len(prev.findings) == 4
    assert [(r.file, r.title, r.head[:1]) for r in prev.refuted] == [
        ("drivers/foo.c", "Lock is not released", "a"),
        ("drivers/Kconfig", "select of BAR is undefined", "a"),
        ("doc/gone.rst", "Title underline too short", "9"),  # carried from a run before that one
    ]
    # Not usable: a run that did not complete, and a record from before the change digests were kept.
    assert parse_record(_record(review=None), _map()) is None
    assert parse_record({k: v for k, v in _record().items() if k != "change"}, _map()) is None
    many = {"concerns": [{"title": f"t {i}", "file": "f.c", "fate": "refuted", "reason": "r"} for i in range(200)]}
    assert len(parse_record(_record(), many).refuted) == MAX_REFUTED
    assert parse_record(_record(), None).refuted == []


def test_a_refutation_stands_only_where_the_file_did_not_change():
    prev = parse_record(_record(), _map())
    now = _file("drivers/foo.c", (10, LOCK), (40, [" \tint ret;", "+\tret = init2();", " \treturn ret;"])) + _file(
        "drivers/Kconfig", (5, [" config FOO", "+\tselect BAR", " \tbool"])
    )
    refuted = settle(prev.refuted, delta_since(prev.change, now, MSG))
    assert [(r.file, r.settled) for r in refuted] == [("drivers/foo.c", False), ("drivers/Kconfig", True)]
    # Settled and restated: reused. A changed file, another file or another point: checked again.
    assert matching_refutation("drivers/Kconfig", "Select of BAR is undefined.", refuted).reason.startswith("BAR is")
    assert matching_refutation("drivers/Kconfig", "The select of BAR is undefined in this tree", refuted)
    assert matching_refutation("drivers/foo.c", "Lock is not released", refuted) is None
    assert matching_refutation("drivers/other", "select of BAR is undefined", refuted) is None
    assert matching_refutation("drivers/Kconfig", "FOO lacks a help text", refuted) is None
    text = refuted_digest(refuted)
    assert '- drivers/Kconfig:6 "select of BAR is undefined" (file unchanged since): BAR is defined' in text
    assert '- drivers/foo.c:12 "Lock is not released" (file changed since): The caller releases it.' in text
    assert [r.to_dict().keys() for r in refuted][0] == {"file", "line", "title", "reason", "head"}


def _zip(files: dict[str, dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, json.dumps(data))
    return buf.getvalue()


def _artifact_gh(artifacts: list[dict], zips: dict[int, bytes], status: int = 200) -> GitHub:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/actions/runs/555/artifacts"):
            return httpx.Response(status, json={"artifacts": artifacts} if status == 200 else {"message": "no"})
        m = re.search(r"/actions/artifacts/(\d+)/zip$", path)
        if m:
            return httpx.Response(200, content=zips[int(m.group(1))])
        return httpx.Response(404, json={"message": path})

    return GitHub("t", "o/r", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_the_record_is_read_from_the_earlier_run_s_artifact():
    logs: list[str] = []
    artifacts = [
        {"id": 1, "name": "build-logs", "size_in_bytes": 10, "expired": False},
        {"id": 2, "name": "ai-review-8-1", "size_in_bytes": 10, "expired": False},  # another pull request
        {"id": 3, "name": "ai-review-7-555", "size_in_bytes": 10, "expired": False},
        {"id": 4, "name": "huge-7", "size_in_bytes": 10**9, "expired": False},
    ]
    zips = {
        1: _zip({"log.json": {}}),
        2: _zip({"review.json": _record(pr=8)}),
        3: _zip({"review-output/review.json": _record(), "review-output/review_map.json": _map()}),
    }
    prev = fetch_run(_artifact_gh(artifacts, zips), "555", 7, logs.append)
    assert prev is not None and prev.head_sha == "a" * 40 and len(prev.refuted) == 3
    assert prev.origin == "artifact 'ai-review-7-555' of run 555"
    # A second attempt of the run leaves two artifacts of one name; the failed attempt's is passed over.
    failed = {"id": 5, "name": "ai-review-7-555", "size_in_bytes": 10, "expired": False}
    again = fetch_run(
        _artifact_gh([failed, artifacts[2]], {**zips, 5: _zip({"review.json": _record(review=None)})}), "555", 7
    )
    assert again is not None and again.head_sha == "a" * 40
    # Expired, of another pull request, unreadable, or not permitted: no record, and no exception.
    assert fetch_run(_artifact_gh([{**artifacts[2], "expired": True}], zips), "555", 7, logs.append) is None
    assert fetch_run(_artifact_gh(artifacts[:2], zips), "555", 7, logs.append) is None
    assert fetch_run(_artifact_gh(artifacts[2:3], {3: b"not a zip"}), "555", 7, logs.append) is None
    assert fetch_run(_artifact_gh(artifacts, zips, status=403), "555", 7, logs.append) is None
    assert any("cannot be listed" in m for m in logs) and any("cannot be read" in m for m in logs)


def test_findings_without_an_inline_comment_are_checked_again_as_claims():
    prev = parse_record(_record(), _map())
    lock, kconf = prev.findings[:2]
    review, _ = parse_review(GOOD_REVIEW, {"drivers/i2c/i2c_foo.c": {17, 18, 19}, "drivers/i2c/Kconfig": {20}})

    class FakeGitHub:
        def review_comments(self, number):
            return [
                {"path": lock["file"], "body": inline_marker(TAG, finding_key(review.findings[0])) + "\n**x** · high"},
                {"path": "drivers/i2c/Kconfig", "body": "a person's comment"},
            ]

    rest = summary_only(prev, FakeGitHub(), 7, TAG)
    assert lock["title"] not in [f["title"] for f in rest] and len(rest) == 3  # the lock finding has its thread
    claims = carried_claims(rest, 4, "a" * 40)
    assert [(c.id, c.file, c.line) for c in claims][0] == ("X4", "drivers/i2c/Kconfig", 20) and len(claims) == 3
    assert claims[0].source == f"{EARLIER_SUMMARY}, at aaaaaaaaaaaa" and claims[0].title == kconf["title"]
    assert "I2C_BUS_RECOVERY is not defined anywhere" in claims[0].text and "thread_id" not in claims[0].extra
    assert claims[0].extra["severity"] == "medium" and claims[0].as_finding()["recommendation"]


def test_the_opening_narrows_the_run_and_lists_what_was_refuted():
    prev = parse_record(_record(), _map())
    now = _file("drivers/foo.c", (10, LOCK), (40, [" \tint ret;", "+\tret = init2();", " \treturn ret;"])) + _file(
        "drivers/Kconfig", (5, [" config FOO", "+\tselect BAR", " \tbool"])
    )
    delta = delta_since(prev.change, now, [Commit(subject="other", body="")])
    scope = scope_section(delta, prev.head_sha, {"test_assessment": "No test covers recovery."})
    assert scope.startswith("### Scope of this run: what changed since the review of `aaaaaaaaaaaa`")
    assert "1 file(s) and the commit messages changed" in scope and "+\tret = init2();" in scope
    assert "xfer()" not in scope and "Unchanged since (1): `drivers/Kconfig`." in scope
    assert "No longer part of the change: `doc/gone.rst`." in scope
    assert "<earlier_note>No test covers recovery.</earlier_note>" in scope
    pr = PullRequest(7, "t", "", "author", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    opening = opening_message(pr, "overview", now, "", "o/r", scope=scope, refuted=refuted_digest(prev.refuted))
    assert opening.index("### Scope of this run") < opening.index("### Full diff (reviewed earlier apart from")
    assert "<refuted_earlier>" in opening and "The caller releases it." in opening
    plain = opening_message(pr, "overview", now, "", "o/r")
    assert "### Full diff\n" in plain and "Scope of this run" not in plain and "refuted_earlier" not in plain


def test_the_summary_names_its_run_and_says_when_it_covers_only_what_changed():
    review, _ = parse_review(GOOD_REVIEW, {"drivers/i2c/i2c_foo.c": {17, 18, 19}, "drivers/i2c/Kconfig": {20}})
    from zephyr_reviewer.review.agent import RunResult
    from zephyr_reviewer.review.openrouter import Usage
    from zephyr_reviewer.review.schema import ClaimVerdict

    run = RunResult(review, 2, Usage(), ["m"], False)
    pr = PullRequest(7, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    review.external_claims = [
        ClaimVerdict("X1", "confirmed", "", "a", "f.c", 1, f"{EARLIER_SUMMARY}, at aaaaaaaaaaaa"),
        ClaimVerdict("X2", "refuted", "gone", "b", "f.c", 2, f"{EARLIER_SUMMARY}, at aaaaaaaaaaaa"),
        ClaimVerdict("X3", "refuted", "gone", "c", "f.c", 3, "the earlier review of aaaaaaaaaaaa"),
    ]
    body = render_comment(review, run, pr, repo="o/r", tag=TAG, run_id="555", since="a" * 40)
    assert run_marker(TAG, "555") in body
    assert "Re-review of what changed since `aaaaaaaaaaaa`; the rest was reviewed then." in body
    assert "Earlier findings from the summary re-checked at this head: 2 (1 still apply" in body
    assert "Earlier inline comments re-checked at this head: 1 (1 resolved" in body
    assert "supplied" not in body.lower()  # neither kind is listed as a claim from outside
    plain = render_comment(review, run, pr, repo="o/r", tag=TAG, run_id="")
    assert ":run:" not in plain and "Re-review" not in plain


def test_the_earlier_record_is_used_only_when_it_is_the_reviewed_head(monkeypatch, capsys):
    from zephyr_reviewer.review.render import head_marker, marker

    class FakeGitHub:
        def __init__(self, body):
            self.body = body

        def find_comment(self, number, mark):
            return {"id": 5, "body": self.body} if self.body else None

    prev = parse_record(_record(), _map(), origin="artifact")
    fetched: list[str] = []
    monkeypatch.setattr(cli, "fetch_run", lambda gh, run, number, log: fetched.append(run) or prev)
    pr = PullRequest(7, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    body = "\n".join([marker(TAG), head_marker(TAG, "a" * 40), run_marker(TAG, "555")])
    delta = ReviewConfig(rereview="delta")

    assert cli._load_previous(ReviewConfig(), FakeGitHub(body), pr) is None and not fetched  # full: not looked up
    assert cli._load_previous(delta, FakeGitHub(body), pr) is prev and fetched == ["555"]
    assert cli._load_previous(delta, FakeGitHub(""), pr) is None  # a first review
    assert cli._load_previous(delta, FakeGitHub(body.replace("a" * 40, "c" * 40)), pr) is None  # a stale record
    assert cli._load_previous(delta, FakeGitHub(body.replace(run_marker(TAG, "555"), "")), pr) is None
    pr.labels = ["AI-Review-Full"]
    assert cli._load_previous(delta, FakeGitHub(body), pr) is None  # a maintainer asked for a fresh look
    err = capsys.readouterr().err
    assert "is not of the reviewed head cccccccccccc" in err and "does not name the run" in err
    assert "label 'ai-review-full' present: full review" in err
    assert any("rereview" in e for e in ReviewConfig(rereview="partial").validate())


# ---------------------------------------------------------------- three runs, end to end
TITLE = GOOD_REVIEW["findings"][0]["title"]


class _Stub(BaseHTTPRequestHandler):
    """Raises one concern from the logic specialist, sends every candidate to a hard case and refutes it."""

    turns: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Stub.turns.append(body)
        names = {t["function"]["name"] for t in body.get("tools", [])}
        first = body["messages"][1]["content"]
        if "submit_concerns" in names:
            stage = first.split("# Stage: ", 1)[1].split("\n", 1)[0]
            concerns = [dict(GOOD_REVIEW["findings"][0], evidence="read_file")] if stage.startswith("execution") else []
            name, args = "submit_concerns", {"concerns": concerns, "dismissed": []}
        elif "submit_verification" in names:
            ids = re.findall(r"^### ([CX]\d+) ", first.split("## Specialist output", 1)[1], re.M)
            cases = [{"source_ids": [i], "question": "Is the lock leaked?", "severity_hint": "high"} for i in ids]
            name, args = "submit_verification", {"findings": [], "dismissed": [], "hard_cases": cases}
        elif "submit_hard_case_verdict" in names:
            name, args = "submit_hard_case_verdict", {"verdict": "refuted", "reason": "The caller releases the lock."}
        else:
            name = "submit_report"
            args = {"summary": "Nothing found.", "verdict": "approve", "confidence": "high"}
        calls = [{"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
        resp = {
            "id": "x",
            "model": "stub/model",
            "choices": [
                {"message": {"role": "assistant", "content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub():
    _Stub.turns = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _git(root, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _review(root, base, head, out, *extra: str) -> list[dict]:
    _Stub.turns = []
    args = ["review", "--repo", str(root), "--base", base, "--head", head, "--dry-run", "--in-process"]
    args += ["--pipeline", "multi-stage", "--draft-model", "cheap/model", "--output-dir", str(out), *extra]
    assert cli.main(args) == 0
    return list(_Stub.turns)


def _terminals(turns: list[dict]) -> list[str]:
    return [
        next(t["function"]["name"] for t in r["tools"] if t["function"]["name"].startswith("submit")) for r in turns
    ]


def _stages(turns: list[dict]) -> set[str]:
    return {
        r["messages"][1]["content"].split("# Stage: ", 1)[1].split("\n", 1)[0]
        for r in turns
        if "# Stage: " in r["messages"][1]["content"] and len(r["messages"]) == 2
    }


def test_three_runs_build_on_each_other(mini_repo, stub, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZR_BASE_URL", stub)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    root = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(mini_repo["root"]), str(root)], check=True)
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "checkout", "-q", mini_repo["head"])
    base = mini_repo["base"]

    # 1. The first review: every specialist the change calls for; the lock concern is investigated and refuted.
    first = _review(root, base, mini_repo["head"], tmp_path / "out1", "--rereview", "delta")
    assert "submit_hard_case_verdict" in _terminals(first) and len(_stages(first)) > 4
    rec1 = json.loads((tmp_path / "out1" / "review.json").read_text())
    assert rec1["review"]["findings"] == [] and "drivers/i2c/i2c_foo.c" in rec1["change"]["files"]
    map1 = json.loads((tmp_path / "out1" / "review_map.json").read_text())
    assert [(c["title"], c["fate"]) for c in map1["concerns"]] == [(TITLE, "refuted")] and "scope" not in map1
    assert run_marker(TAG, "555") in (tmp_path / "out1" / "comment.md").read_text()
    capsys.readouterr()

    # 2. A push that touches the documentation only: the delta is reviewed by the specialists it calls for,
    # and the lock concern, raised again on code that did not change, keeps its verdict without a new check.
    (root / "doc/hardware/peripherals/i2c.rst").write_text("I2C\n===\n\nBus recovery is supported.\n")
    _git(root, "commit", "-q", "-am", "doc: i2c: mention bus recovery\n\nSigned-off-by: T <t@example.com>")
    head2 = _git(root, "rev-parse", "HEAD")
    second = _review(
        root, base, head2, tmp_path / "out2", "--rereview", "delta", "--previous-review", str(tmp_path / "out1")
    )
    assert _terminals(second).count("submit_concerns") == 2 and len(_stages(second)) == 2  # intent and logic
    assert "submit_verification" not in _terminals(second) and "submit_hard_case_verdict" not in _terminals(second)
    opening = second[0]["messages"][1]["content"]
    assert "### Scope of this run: what changed since the review of" in opening
    assert "+Bus recovery is supported." in opening.split("### Full diff", 1)[0]
    assert (
        "<refuted_earlier>" in opening and f'"{TITLE}" (file unchanged since): The caller releases the lock.' in opening
    )
    err = capsys.readouterr().err
    assert "reviewing what changed since: 1 of " in err and "and the commit messages" in err
    assert "1 concern(s) restate candidates an earlier run refuted" in err
    map2 = json.loads((tmp_path / "out2" / "review_map.json").read_text())
    assert map2["scope"] == {
        "since": mini_repo["head"],
        "narrowed": True,
        "files": ["doc/hardware/peripherals/i2c.rst"],
    }
    assert [(c["fate"], c["by"]) for c in map2["concerns"]] == [("refuted", "earlier review")]
    assert [r["title"] for r in map2["refuted_earlier"]] == [TITLE]
    comment2 = (tmp_path / "out2" / "comment.md").read_text()
    assert f"Re-review of what changed since `{mini_repo['head'][:12]}`" in comment2

    # 3. A push that changes the driver: the refutation is known but no longer settled, so the concern is
    # investigated again. The record of run 2 carried it, although run 2 did not investigate it itself.
    src = root / "drivers/i2c/i2c_foo.c"
    src.write_text(src.read_text() + "\n/* reworked */\n")
    _git(root, "commit", "-q", "-am", "drivers: i2c: foo: rework\n\nSigned-off-by: T <t@example.com>")
    head3 = _git(root, "rev-parse", "HEAD")
    third = _review(
        root, base, head3, tmp_path / "out3", "--rereview", "delta", "--previous-review", str(tmp_path / "out2")
    )
    assert "submit_verification" in _terminals(third) and "submit_hard_case_verdict" in _terminals(third)
    assert f'"{TITLE}" (file changed since)' in third[0]["messages"][1]["content"]
    assert "Bus recovery is supported" not in third[0]["messages"][1]["content"].split("### Full diff", 1)[0]
    capsys.readouterr()

    # Without the opt-in the same push is a full review that reads nothing back.
    full = _review(root, base, head3, tmp_path / "out4", "--previous-review", str(tmp_path / "out2"))
    assert "Scope of this run" not in full[0]["messages"][1]["content"] and len(_stages(full)) > 4
    assert "refuted_earlier" not in full[0]["messages"][1]["content"]


def test_a_push_that_rewrites_most_of_the_change_gets_a_full_review_that_still_reuses_verdicts(
    mini_repo, stub, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ZR_BASE_URL", stub)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    root = mini_repo["root"]
    out1 = tmp_path / "out1"
    _review(root, mini_repo["base"], mini_repo["head"], out1)
    # Pretend the earlier review saw only the Kconfig file: everything else is new since.
    rec = json.loads((out1 / "review.json").read_text())
    rec["head_sha"] = "a" * 40
    rec["change"]["files"] = {k: v for k, v in rec["change"]["files"].items() if k.endswith("Kconfig")}
    (out1 / "review.json").write_text(json.dumps(rec))
    m = json.loads((out1 / "review_map.json").read_text())
    m["concerns"] = [{"title": "select without a definition", "file": "drivers/i2c/Kconfig", "fate": "refuted",
                      "reason": "Defined elsewhere."}]  # fmt: skip
    (out1 / "review_map.json").write_text(json.dumps(m))
    capsys.readouterr()
    turns = _review(
        root,
        mini_repo["base"],
        mini_repo["head"],
        tmp_path / "out2",
        "--rereview",
        "delta",
        "--previous-review",
        str(out1),
    )
    err = capsys.readouterr().err
    assert "of the changed lines are new since then: full review" in err
    opening = turns[0]["messages"][1]["content"]
    assert "Scope of this run" not in opening and len(_stages(turns)) > 4
    assert '"select without a definition" (file unchanged since): Defined elsewhere.' in opening
    assert "Re-review of what changed" not in (tmp_path / "out2" / "comment.md").read_text()
