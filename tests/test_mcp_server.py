# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""The MCP surface, exercised in-process and over a real stdio subprocess."""

from __future__ import annotations

import sys

from mcp import Client

from zephyr_reviewer.intel import SourceIntel
from zephyr_reviewer.mcp_server import build_server
from zephyr_reviewer.review.mcp_client import ToolBridge, stdio_params

EXPECTED_TOOLS = {
    "pr_overview",
    "commit_messages",
    "file_diff",
    "read_file",
    "read_file_at_base",
    "search",
    "list_files",
    "find_definition",
    "find_references",
    "kconfig_symbol",
    "dt_binding",
    "maintainers",
    "related_tests",
    "file_history",
    "blame",
    "project_guidelines",
    "test_candidates",
    "list_platforms",
}


async def test_tools_in_process(mini_repo):
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    async with Client(build_server(intel)) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == EXPECTED_TOOLS
        res = await client.call_tool("find_definition", {"name": "foo_transfer"})
        assert not res.is_error and "[function] drivers/i2c/i2c_foo.c:10" in res.content[0].text
        res = await client.call_tool("read_file", {"path": "drivers/i2c/Kconfig", "start_line": 1, "end_line": 3})
        assert "lines 1-3 of" in res.content[0].text
        # A failing lookup is reported to the model, not raised as a server crash.
        res = await client.call_tool("blame", {"path": "nope.c", "start_line": 1, "end_line": 2})
        assert "No blame output" in res.content[0].text or res.is_error


async def test_bridge_exposes_openai_schemas_and_truncates(mini_repo):
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    async with ToolBridge(build_server(intel), max_result_chars=120) as bridge:
        assert bridge.tool_names == EXPECTED_TOOLS
        read = next(t for t in bridge.tools if t["function"]["name"] == "read_file")
        params = read["function"]["parameters"]
        assert params["type"] == "object" and "title" not in params
        assert params["properties"]["end_line"]["type"] == "integer"  # anyOf[int, null] collapsed
        assert params["required"] == ["path"]
        text = await bridge.call("pr_overview", {})
        assert len(text) <= 200 and "truncated to 120" in text
        err = await bridge.call("read_file", {"path": "x", "start_line": "not-an-int"})
        assert err.startswith("ERROR")
        assert [c.name for c in bridge.calls] == ["pr_overview", "read_file"]
        assert bridge.calls[1].is_error


async def test_bridge_over_stdio_subprocess(mini_repo):
    params = stdio_params(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"], 24_000, python=sys.executable)
    async with ToolBridge(params) as bridge:
        assert "maintainers" in bridge.tool_names
        text = await bridge.call("maintainers", {"path": "drivers/i2c/i2c_foo.c"})
        assert "@alice" in text
        # An argument holding a lone surrogate (what json.loads makes of a model's "\ud83d") must not take
        # the transport down: it is replaced, the call is answered, and the session goes on.
        text = await bridge.call("search", {"pattern": "foo_transfer\ud83d"})
        assert "\ud83d" not in text
        text = await bridge.call("maintainers", {"path": "drivers/i2c/i2c_foo.c"})
        assert "@alice" in text


def test_the_server_subprocess_finds_the_package_as_this_process_did(monkeypatch):
    # The action runs the reviewer from its checkout through PYTHONPATH, without installing it. The MCP
    # client does not pass that variable on by itself, and the server would not start.
    monkeypatch.setenv("PYTHONPATH", "/action/src")
    params = stdio_params("/repo", "h", "b", 1000)
    assert params.env == {"PYTHONPATH": "/action/src"}
    # The working directory is the repository under review: it must not be importable.
    assert params.args[:3] == ["-P", "-m", "zephyr_reviewer.mcp_server"]
    monkeypatch.delenv("PYTHONPATH")
    assert stdio_params("/repo", "h", "b", 1000).env is None
