# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Large inputs and model failures end cleanly: compaction, clean errors, quiet server shutdown."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.test_agent import GOOD_REVIEW, _completion, _tool_call
from zephyr_reviewer import cli
from zephyr_reviewer.intel import SourceIntel
from zephyr_reviewer.mcp_server import _only_broken_pipes, build_server
from zephyr_reviewer.review.agent import ELIDED, compact_history, run_review
from zephyr_reviewer.review.mcp_client import ToolBridge
from zephyr_reviewer.review.openrouter import ContextLengthError, OpenRouter, OpenRouterError


def _llm(handler, models=("m",)) -> OpenRouter:
    llm = OpenRouter("k", list(models), client=httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=1)
    llm._sleep = lambda *a: None  # type: ignore[method-assign]
    return llm


def test_context_length_errors_are_classified():
    def too_long(request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "This endpoint's maximum context length is 128000 tokens. "
                    "However, you requested 131000 tokens.",
                    "code": 400,
                }
            },
        )

    with pytest.raises(ContextLengthError):
        _llm(too_long).chat([{"role": "user", "content": "x"}])

    def other(request):
        return httpx.Response(400, json={"error": {"message": "Invalid tool schema", "code": 400}})

    with pytest.raises(OpenRouterError) as info:
        _llm(other).chat([{"role": "user", "content": "x"}])
    assert not isinstance(info.value, ContextLengthError)

    def http_413(request):
        return httpx.Response(413, text="prompt is too long")

    with pytest.raises(ContextLengthError):
        _llm(http_413).chat([{"role": "user", "content": "x"}])


def test_compact_history_elides_older_tool_results_only():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": []})
        msgs.append({"role": "tool", "tool_call_id": str(i), "name": "read_file", "content": f"result {i}"})
    assert compact_history(msgs, keep_recent=4) == 2
    tool_contents = [m["content"] for m in msgs if m["role"] == "tool"]
    assert tool_contents == [ELIDED, ELIDED, "result 2", "result 3", "result 4", "result 5"]
    assert compact_history(msgs, keep_recent=4) == 0  # nothing older left
    assert compact_history(msgs, keep_recent=0) == 4  # everything else
    assert msgs[0]["content"] == "s" and msgs[1]["content"] == "u"


async def test_agent_compacts_on_context_overflow_then_submits(mini_repo):
    calls = {"n": 0}
    seen: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        calls["n"] += 1
        n = calls["n"]
        if n == 1:  # ask for three tool results
            return httpx.Response(
                200,
                json=_completion(
                    {
                        "content": "",
                        "tool_calls": [
                            _tool_call(
                                "a", "read_file", {"path": "drivers/i2c/i2c_foo.c", "start_line": 1, "end_line": 10}
                            ),
                            _tool_call(
                                "b", "read_file", {"path": "drivers/i2c/Kconfig", "start_line": 1, "end_line": 10}
                            ),
                            _tool_call("c", "maintainers", {"path": "drivers/i2c/i2c_foo.c"}),
                            _tool_call("d", "pr_overview", {}),
                            _tool_call("e", "commit_messages", {}),
                            _tool_call("f", "file_diff", {"path": "drivers/i2c/Kconfig"}),
                        ],
                    }
                ),
            )
        if n == 2:  # the transcript no longer fits
            return httpx.Response(400, json={"error": {"message": "maximum context length exceeded", "code": 400}})
        return httpx.Response(
            200, json=_completion({"content": "", "tool_calls": [_tool_call("z", "submit_review", GOOD_REVIEW)]})
        )

    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_review(
            _llm(handler), bridge, "sys", "open", {f.path: f.new_side_lines for f in intel.files}, log=lambda m: None
        )
    assert run.review is not None and run.errors == []
    # The retried request carried elided older results and the four most recent intact.
    retried = [m for m in seen[2]["messages"] if m["role"] == "tool"]
    assert [m["content"] == ELIDED for m in retried] == [True, True, False, False, False, False]
    assert any(t.get("kind") == "compaction" and t["elided"] == 2 for t in run.transcript)


async def test_agent_forces_submission_when_compaction_is_not_enough(mini_repo):
    calls = {"n": 0}
    seen: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            return httpx.Response(
                200,
                json=_completion(
                    {
                        "content": "",
                        "tool_calls": [
                            _tool_call(
                                str(i), "read_file", {"path": "drivers/i2c/i2c_foo.c", "start_line": 1, "end_line": 3}
                            )
                            for i in range(6)
                        ],
                    }
                ),
            )
        if n in (2, 3):  # overflow twice: once after eliding older results, once more after that
            return httpx.Response(
                400, json={"error": {"message": "context window too small for this request", "code": 400}}
            )
        return httpx.Response(
            200, json=_completion({"content": "", "tool_calls": [_tool_call("z", "submit_review", GOOD_REVIEW)]})
        )

    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_review(
            _llm(handler), bridge, "sys", "open", {f.path: f.new_side_lines for f in intel.files}, log=lambda m: None
        )
    assert run.review is not None and run.forced
    assert seen[3]["tool_choice"] == {"type": "function", "function": {"name": "submit_review"}}
    assert all(m["content"] == ELIDED for m in seen[3]["messages"] if m["role"] == "tool")
    assert [t["elided"] for t in run.transcript if t.get("kind") == "compaction"] == [2, 4]


async def test_agent_gives_up_when_nothing_is_left_to_compact(mini_repo):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json=_completion(
                    {
                        "content": "",
                        "tool_calls": [
                            _tool_call(
                                "a", "read_file", {"path": "drivers/i2c/i2c_foo.c", "start_line": 1, "end_line": 5}
                            )
                        ],
                    }
                ),
            )
        return httpx.Response(400, json={"error": {"message": "maximum context length exceeded", "code": 400}})

    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_review(_llm(handler), bridge, "sys", "open", {}, log=lambda m: None)
    assert run.review is None and run.errors[0].startswith("context window exceeded and nothing left to compact")
    assert calls["n"] == 3  # tool turn, overflow (elide all, force), overflow (give up)


async def test_agent_returns_cleanly_on_other_model_errors(mini_repo):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "Invalid tool schema", "code": 400}})

    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_review(_llm(handler), bridge, "sys", "open", {}, log=lambda m: None)
    assert run.review is None and run.errors and run.errors[0].startswith("model request failed: ")


def test_broken_pipe_classification():
    assert _only_broken_pipes(BrokenPipeError())
    assert _only_broken_pipes(BaseExceptionGroup("g", [BrokenPipeError(), BrokenPipeError()]))
    assert _only_broken_pipes(BaseExceptionGroup("g", [BaseExceptionGroup("inner", [BrokenPipeError()])]))
    assert not _only_broken_pipes(BaseExceptionGroup("g", [BrokenPipeError(), ValueError("real")]))
    assert not _only_broken_pipes(ValueError("real"))
    assert not _only_broken_pipes(BaseExceptionGroup("empty", [KeyboardInterrupt()]))


def test_review_command_reports_a_failed_run_without_a_traceback(mini_repo, tmp_path, monkeypatch, capsys):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Bad(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            data = json.dumps({"error": {"message": "Invalid tool schema", "code": 400}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Bad)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ZR_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test")
        out = tmp_path / "out"
        rc = cli.main(
            [
                "review",
                "--repo",
                str(mini_repo["root"]),
                "--base",
                mini_repo["base"],
                "--head",
                mini_repo["head"],
                "--dry-run",
                "--in-process",
                "--output-dir",
                str(out),
            ]
        )
    finally:
        server.shutdown()
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "model request failed" in err
    record = json.loads((out / "review.json").read_text())
    assert record["review"] is None and record["errors"] and "did not complete" in (out / "comment.md").read_text()


def test_model_output_with_lone_surrogates_is_made_encodable():
    from zephyr_reviewer.review.text import without_surrogates

    nested = {"a": ["x\ud83dy", 1, {"b": "ok"}], "c": None}
    assert without_surrogates(nested) == {"a": ["x?y", 1, {"b": "ok"}], "c": None}
    assert without_surrogates("plain \U0001f600 text") == "plain \U0001f600 text"

    def handler(request):
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"pattern": "foo\\ud83d"}'},
        }
        message = {"role": "assistant", "content": "see \ud83d here", "tool_calls": [call]}
        body = {"model": "m", "choices": [{"message": message, "finish_reason": "tool_calls"}], "usage": {}}
        return httpx.Response(200, content=json.dumps(body).encode())  # ensure_ascii escapes the surrogate

    result = _llm(handler).chat([{"role": "user", "content": "hi"}])
    assert result.message["content"] == "see ? here"
    json.dumps(result.message).encode("utf-8")  # what reaches the transcript and the next request encodes
    # the arguments are still JSON text here; the escape becomes a surrogate when the agent parses them
    from zephyr_reviewer.review.agent import tool_arguments

    raw = result.tool_calls[0]["function"]["arguments"]
    assert json.loads(raw) == {"pattern": "foo\ud83d"} and tool_arguments(raw) == {"pattern": "foo?"}
