# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""The specialist pipeline: planning, accounting rules, and a full scripted run."""

from __future__ import annotations

import json
import re

import httpx

from tests.test_agent import ScriptedOpenRouter, _completion, _llm
from zephyr_reviewer.intel import SourceIntel
from zephyr_reviewer.mcp_server import build_server
from zephyr_reviewer.review.external import Claim
from zephyr_reviewer.review.github_api import PullRequest
from zephyr_reviewer.review.mcp_client import ToolBridge
from zephyr_reviewer.review.pipeline import (
    Concern,
    agreement_line,
    hunk_text,
    make_verification_parser,
    merge_concern,
    plan_specialists,
    run_pipeline,
    verification_message,
)
from zephyr_reviewer.review.prompts import load_profile, opening_message, system_prompt
from zephyr_reviewer.review.schema import parse_review


def test_planner_picks_passes_from_the_change():
    assert plan_specialists(["doc/x.rst"], "") == ["intent", "logic"]
    # Kernel C code always gets the security pass: syscall verifiers and object checks live there.
    assert plan_specialists(["kernel/sched.c"], "z_sched_lock();") == [
        "intent",
        "logic",
        "error_handling",
        "concurrency",
        "security",
        "coding_guidelines",
    ]
    assert plan_specialists(["arch/arm/core/fault.S"], "") == [
        "intent",
        "logic",
        "error_handling",
        "concurrency",
        "coding_guidelines",
    ]
    assert plan_specialists(["lib/os/clock.c"], "") == [
        "intent",
        "logic",
        "error_handling",
        "resources",
        "security",
        "coding_guidelines",
    ]
    assert plan_specialists(["Kconfig"], "") == ["intent", "logic", "build_config"]  # no C, no guidelines pass
    assert plan_specialists(["drivers/i2c/i2c_foo.c", "drivers/i2c/Kconfig"], "k_sem_take(&l, K_FOREVER);") == [
        "intent",
        "logic",
        "error_handling",
        "concurrency",
        "resources",
        "hardware",
        "build_config",
        "coding_guidelines",
    ]
    assert "security" in plan_specialists(["subsys/net/ip/tcp.c"], "memcpy(a, b, n);")
    # Security runs for the attacker-facing areas the sightings came from, and on wire-length idioms anywhere.
    for path, text in [
        ("drivers/ieee802154/ieee802154_rf2xx.c", "rx_len = phr & 0x7f;"),
        ("subsys/fs/ext2/ext2_ops.c", ""),
        ("subsys/llext/llext_load.c", ""),
        ("kernel/poll.c", "z_vrfy_k_poll"),
        ("subsys/usb/device_next/class/usbd_cdc_ncm.c", ""),
        ("drivers/modem/hl7800.c", ""),
        ("subsys/lorawan/services/frag_transport.c", ""),
        ("drivers/foo/anything.c", "len = sys_get_le16(buf); memcpy(dst, buf, len);"),
    ]:
        assert "security" in plan_specialists([path], text), path
    assert "security" not in plan_specialists(["drivers/gpio/gpio_foo.c"], "gpio_pin_set(dev, 1);")
    assert "security" not in plan_specialists(["doc/x.rst"], "memcpy")  # no C code
    assert "build_config" in plan_specialists(["boards/vnd/b/b.dts"], "")
    assert "hardware" not in plan_specialists(["boards/vnd/b/b.dts"], "")  # no C code
    assert plan_specialists(["dts/bindings/i2c/x.yaml"], "") == ["intent", "logic", "build_config"]
    assert "error_handling" in plan_specialists(["subsys/net/ip/tcp.c"], "")


def _finding(title="t", **kw):
    f = {
        "file": "a.c",
        "line": 1,
        "severity": "medium",
        "category": "bug",
        "title": title,
        "problem": "p",
        "trigger": "",
        "impact": "",
        "recommendation": "",
        "confidence": "medium",
    }
    f.update(kw)
    return f


def test_verification_parser_enforces_full_accounting():
    parse = make_verification_parser({"C1", "C2", "C3"}, {"D1", "D2"})
    ok, errs = parse(
        {
            "findings": [{**_finding("merged"), "source_ids": ["C1", "C2"]}],
            "dismissed": [{"source_ids": ["D1"], "reason": "guarded"}],
            "hard_cases": [{"source_ids": ["C3", "D2"], "question": "is the lock held?", "severity_hint": "high"}],
        }
    )
    assert errs == [] and ok is not None
    assert [h.id for h in ok.hard_cases] == ["H1"] and ok.hard_cases[0].source_ids == ["C3", "D2"]

    # Missing ids are rejected, naming them.
    _, errs = parse({"findings": [{**_finding(), "source_ids": ["C1"]}], "dismissed": [], "hard_cases": []})
    assert errs == ["every id must be accounted for; missing: C2, C3, D1, D2"]
    # A concern may not be dismissed directly; a dismissal may not be confirmed as a finding.
    _, errs = parse(
        {
            "findings": [{**_finding(), "source_ids": ["D1"]}],
            "dismissed": [{"source_ids": ["C1"], "reason": "nah"}],
            "hard_cases": [{"source_ids": ["C2", "C3", "D2"], "question": "", "severity_hint": "low"}],
        }
    )
    assert "findings[0]: D1 is not allowed in findings (concerns you doubt go to hard_cases)" in errs
    assert "dismissed[0]: C1 is not allowed in dismissed (concerns you doubt go to hard_cases)" in errs
    # Double placement and unknown ids are rejected.
    _, errs = parse(
        {
            "findings": [{**_finding(), "source_ids": ["C1"]}, {**_finding(), "source_ids": ["C1", "C9"]}],
            "dismissed": [{"source_ids": ["D1", "D2"], "reason": "r"}],
            "hard_cases": [{"source_ids": ["C2", "C3"], "question": "", "severity_hint": "medium"}],
        }
    )
    assert "C1 appears in both findings[0] and findings[1]" in errs and "findings[1]: unknown id 'C9'" in errs


def test_speculative_findings_are_capped_at_medium():
    review, errs = parse_review(
        {
            "summary": "s",
            "verdict": "comment",
            "confidence": "high",
            "findings": [_finding("spec", severity="critical", speculative=True), _finding("solid", severity="high")],
        },
        {"a.c": {1}},
    )
    assert not errs and review is not None
    spec, solid = review.findings
    assert spec.speculative and spec.severity == "medium" and "capped at medium (was critical)" in spec.notes[0]
    assert not solid.speculative and solid.severity == "high"


# --- full pipeline against a scripted model ------------------------------------------------------
def _tc(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class StageRouter:
    """Answers each request by the stage named in its system prompt or by the terminal tool offered."""

    def __init__(self, verification_first_attempt_bad: bool = True):
        self.requests: list[dict] = []
        self.bad_once = verification_first_attempt_bad
        self.verification_attempts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        system = body["messages"][0]["content"]
        tools = {t["function"]["name"] for t in body["tools"]}
        if "submit_concerns" in tools:
            stage = system.split("# Stage: ", 1)[1].split("\n", 1)[0].strip()
            if stage.startswith("intent"):
                out = {
                    "concerns": [
                        _finding(
                            "Commit message does not mention the Kconfig symbol",
                            file="drivers/i2c/Kconfig",
                            line=20,
                            severity="low",
                            category="commit_message",
                            evidence="commit_messages",
                        )
                    ],
                    "dismissed": [],
                }
            elif stage.startswith("concurrency"):
                out = {
                    "concerns": [
                        _finding(
                            "Early return leaks the bus lock",
                            file="drivers/i2c/i2c_foo.c",
                            line=17,
                            end_line=19,
                            severity="high",
                            category="concurrency",
                            trigger="failed transfer",
                            impact="bus wedged",
                            recommendation="give the sem",
                            evidence="read_file drivers/i2c/i2c_foo.c:9-21",
                            confidence="high",
                        )
                    ],
                    "dismissed": [
                        {
                            "title": "k_sem_init count",
                            "file": "drivers/i2c/i2c_foo.c",
                            "line": 32,
                            "reason": "binary semaphore is intended",
                            "evidence": "read_file :28-34",
                        }
                    ],
                }
            elif stage.startswith("Kconfig"):
                out = {
                    "concerns": [
                        _finding(
                            "select of undefined I2C_BUS_RECOVERY",
                            file="drivers/i2c/Kconfig",
                            line=20,
                            severity="medium",
                            category="build_config",
                            evidence="kconfig_symbol",
                        )
                    ],
                    "dismissed": [
                        {
                            "title": "I2C_FOO default y",
                            "file": "drivers/i2c/Kconfig",
                            "line": 12,
                            "reason": "gated by DT_HAS",
                            "evidence": "kconfig_symbol I2C_FOO",
                        }
                    ],
                }
            else:
                out = {"concerns": [], "dismissed": []}
            calls = [_tc("c", "submit_concerns", out)]
        elif "submit_verification" in tools:
            self.verification_attempts += 1
            if self.bad_once and self.verification_attempts == 1:
                # Forgets D2 and tries to dismiss a concern directly: must be rejected.
                calls = [
                    _tc(
                        "v",
                        "submit_verification",
                        {
                            "findings": [
                                {
                                    **_finding(
                                        "Early return leaks the bus lock",
                                        file="drivers/i2c/i2c_foo.c",
                                        line=17,
                                        end_line=19,
                                        severity="high",
                                        category="concurrency",
                                        confidence="high",
                                    ),
                                    "source_ids": ["C2"],
                                }
                            ],
                            "dismissed": [
                                {"source_ids": ["C1"], "reason": "not a code issue"},
                                {"source_ids": ["D1"], "reason": "ok"},
                            ],
                            "hard_cases": [
                                {
                                    "source_ids": ["C3"],
                                    "question": "is I2C_BUS_RECOVERY defined?",
                                    "severity_hint": "medium",
                                }
                            ],
                        },
                    )
                ]
            else:
                calls = [
                    _tc(
                        "v",
                        "submit_verification",
                        {
                            "findings": [
                                {
                                    **_finding(
                                        "Early return leaks the bus lock",
                                        file="drivers/i2c/i2c_foo.c",
                                        line=17,
                                        end_line=19,
                                        severity="high",
                                        category="concurrency",
                                        confidence="high",
                                        evidence="read_file i2c_foo.c:9-21",
                                    ),
                                    "source_ids": ["C2"],
                                }
                            ],
                            "dismissed": [{"source_ids": ["D1", "D2"], "reason": "both cite disproving code"}],
                            "hard_cases": [
                                {
                                    "source_ids": ["C3"],
                                    "question": "is I2C_BUS_RECOVERY defined anywhere?",
                                    "severity_hint": "medium",
                                },
                                {
                                    "source_ids": ["C1"],
                                    "question": "is the message really incomplete?",
                                    "severity_hint": "low",
                                },
                            ],
                        },
                    )
                ]
        elif "submit_hard_case_verdict" in tools:
            case_text = body["messages"][1]["content"].split("## Hard case", 1)[1]
            if "I2C_BUS_RECOVERY" in case_text:
                calls = [
                    _tc(
                        "h",
                        "submit_hard_case_verdict",
                        {
                            "verdict": "confirmed",
                            "reason": "kconfig_symbol shows no definition",
                            "finding": _finding(
                                "select of undefined I2C_BUS_RECOVERY",
                                file="drivers/i2c/Kconfig",
                                line=20,
                                severity="critical",
                                category="build_config",
                                speculative=True,
                                evidence="kconfig_symbol I2C_BUS_RECOVERY: no definition",
                            ),
                        },
                    )
                ]
            else:
                calls = [
                    _tc(
                        "h",
                        "submit_hard_case_verdict",
                        {"verdict": "refuted", "reason": "the second commit message names the symbol"},
                    )
                ]
        elif "submit_report" in tools:
            assert body.get("tools") and len(body["tools"]) <= 2, "report stage must not get the source tools"
            calls = [
                _tc(
                    "r",
                    "submit_report",
                    {
                        "summary": "The error path leaks the bus lock.",
                        "verdict": "comment",
                        "confidence": "high",
                        "test_assessment": "Tests are adequate.",
                        "commit_message_assessment": "",
                        "discussion_assessment": "",
                    },
                )
            ]
        else:
            raise AssertionError(f"unexpected tools {tools}")
        return httpx.Response(200, json=_completion({"content": "", "tool_calls": calls}, prompt_tokens=200))


async def test_pipeline_end_to_end(mini_repo):
    router = StageRouter()
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(7, "t", "", "author", "main", mini_repo["base"], "pr", mini_repo["head"], "o/r", False)
    opening = opening_message(pr, intel.overview(), intel.diff_text, "", "o/r")
    added = hunk_text(intel.diff_text)
    llm_cheap = _llm(ScriptedOpenRouter([]), models=("cheap/model",))
    llm_cheap._client = httpx.Client(transport=httpx.MockTransport(router.handler))
    llm_strong = _llm(ScriptedOpenRouter([]), models=("strong/model",))
    llm_strong._client = httpx.Client(transport=httpx.MockTransport(router.handler))
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_pipeline(
            llm_cheap,
            llm_strong,
            bridge,
            system_prompt(load_profile("zephyr"), "English"),
            opening,
            {f.path: f.new_side_lines for f in intel.files},
            changed_files=[f.path for f in intel.files],
            added_text=added,
            log=lambda m: None,
        )

    assert run.review is not None
    rm = run.review_map
    assert rm["specialists"] == [
        "intent",
        "logic",
        "error_handling",
        "concurrency",
        "resources",
        "hardware",
        "build_config",
        "coding_guidelines",
    ]
    # Specialist models vs verification models.
    spec_models = {
        r["model"] for r in router.requests if "submit_concerns" in {t["function"]["name"] for t in r["tools"]}
    }
    ver_models = {
        r["model"] for r in router.requests if "submit_verification" in {t["function"]["name"] for t in r["tools"]}
    }
    assert spec_models == {"cheap/model"} and ver_models == {"strong/model"}
    # The first verification attempt was rejected for breaking the accounting rules, then accepted.
    assert router.verification_attempts == 2
    vreqs = [r for r in router.requests if "submit_verification" in {t["function"]["name"] for t in r["tools"]}]
    rejection = [m for m in vreqs[1]["messages"] if m["role"] == "tool"]
    assert any("missing: D2" in m["content"] and "C1 is not allowed in dismissed" in m["content"] for m in rejection)

    fates = {c["id"]: c for c in rm["concerns"]}
    assert fates["C2"]["fate"] == "confirmed" and fates["C2"]["by"] == "verification"
    assert fates["C3"]["fate"] == "confirmed" and fates["C3"]["by"].startswith("H")
    assert fates["C1"]["fate"] == "refuted"
    assert {d["fate"] for d in rm["dismissed_concerns"]} == {"dismissed"}
    assert all(c["fate"] != "unaccounted" for c in rm["concerns"] + rm["dismissed_concerns"])

    titles = [f.title for f in run.review.findings]
    assert titles == ["Early return leaks the bus lock", "select of undefined I2C_BUS_RECOVERY"]
    lock, kconf = run.review.findings
    assert lock.inline and lock.line == 17 and "[C2]" in lock.evidence
    assert kconf.speculative and kconf.severity == "medium" and "[C3]" in kconf.evidence  # critical capped
    # The report said "comment" with a high finding standing: the deterministic rule wins.
    assert run.review.verdict == "request_changes"
    assert run.review.summary == "The error path leaks the bus lock." and run.review.test_assessment == ""
    stage_names = [s["stage"] for s in run.stages]
    n = len(rm["specialists"])
    assert stage_names[:n] == rm["specialists"] and "verification" in stage_names and "report" in stage_names
    assert sum(1 for s in stage_names if s.startswith("H")) == 2
    assert run.usage.requests == len(router.requests) and run.steps == sum(s["steps"] for s in run.stages)
    assert {t.get("stage") for t in run.transcript} >= {"intent", "verification", "report", "H1", "H2"}


async def test_pipeline_with_no_concerns_skips_verification(mini_repo):
    class Quiet(StageRouter):
        def handler(self, request):
            body = json.loads(request.content)
            self.requests.append(body)
            tools = {t["function"]["name"] for t in body["tools"]}
            if "submit_concerns" in tools:
                calls = [_tc("c", "submit_concerns", {"concerns": [], "dismissed": []})]
            else:
                assert "submit_report" in tools
                calls = [
                    _tc(
                        "r",
                        "submit_report",
                        {
                            "summary": "No issues found.",
                            "verdict": "request_changes",
                            "confidence": "high",
                            "test_assessment": "",
                            "commit_message_assessment": "",
                            "discussion_assessment": "",
                        },
                    )
                ]
            return httpx.Response(200, json=_completion({"content": "", "tool_calls": calls}))

    router = Quiet()
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(0, "t", "", "", "main", mini_repo["base"], "pr", mini_repo["head"], "", False)
    llm = _llm(ScriptedOpenRouter([]), models=("m",))
    llm._client = httpx.Client(transport=httpx.MockTransport(router.handler))
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_pipeline(
            llm,
            llm,
            bridge,
            "sys",
            opening_message(pr, "ov", None, "", ""),
            {},
            changed_files=["doc/x.rst"],
            added_text="",
            specialists=["intent"],
            log=lambda m: None,
        )
    assert run.review is not None and run.review.findings == []
    assert run.review.verdict == "approve"  # request_changes without findings is corrected
    assert [s["stage"] for s in run.stages] == ["intent", "report"]
    assert run.review_map["concerns"] == [] and run.errors == []


def test_error_handling_stage_prompt_covers_returns_and_asserts():
    from zephyr_reviewer.review.pipeline import _stage_prompt

    text = _stage_prompt("error_handling")
    assert text.startswith("# Stage: error handling and assertions")
    for must in (
        "device_is_ready",
        "k_sem_take",
        "net_buf_alloc",
        "errno",
        "release what the success path would release",
        "CONFIG_ASSERT",
        "compiled out",
        "invariant",
        "runtime check",
        "syscall argument",
    ):
        assert must in text
    # The logic stage no longer claims the same ground.
    assert "belong to the error-handling stage" in _stage_prompt("logic")


def test_refuted_candidates_section_is_opt_in():
    from tests.test_agent import GOOD_REVIEW
    from zephyr_reviewer.review.agent import RunResult
    from zephyr_reviewer.review.github_api import PullRequest
    from zephyr_reviewer.review.openrouter import Usage
    from zephyr_reviewer.review.render import refuted_candidates, render_comment

    review, _ = parse_review(GOOD_REVIEW, {"drivers/i2c/i2c_foo.c": {17, 18, 19}, "drivers/i2c/Kconfig": {20}})
    rm = {
        "specialists": ["intent"],
        "concerns": [
            {"id": "C1", "stage": "intent", "title": "Kept", "fate": "confirmed", "by": "verification"},
            {
                "id": "C2",
                "stage": "security",
                "title": "Deferred close changes lifetime semantics",
                "fate": "refuted",
                "by": "H1",
                "reason": "the walker frees the node under ilock; callers never observe a freed session.",
            },
            {"id": "C3", "stage": "logic", "title": "Unused", "fate": "dismissed"},
        ],
        "dismissed_concerns": [],
        "findings": [],
        "stages": [],
    }
    run = RunResult(review, 3, Usage(), ["m"], False, review_map=rm)
    pr = PullRequest(7, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    assert refuted_candidates(run) == [
        (
            "Deferred close changes lifetime semantics",
            "security",
            "the walker frees the node under ilock; callers never observe a freed session.",
        )
    ]
    plain = render_comment(review, run, pr, repo="o/r", tag="t")
    assert "Also considered and refuted" not in plain
    shown = render_comment(review, run, pr, repo="o/r", tag="t", refuted=True)
    assert "<details><summary>Also considered and refuted: 1 candidate(s)</summary>" in shown
    assert "- Deferred close changes lifetime semantics (security): the walker frees the node under ilock" in shown
    assert "Kept" not in shown.split("Also considered")[1]
    # No review map (single agent): nothing added even when asked.
    assert "Also considered" not in render_comment(
        review, RunResult(review, 1, Usage(), ["m"], False), pr, repo="o/r", tag="t", refuted=True
    )


def test_security_stage_prompt_encodes_the_sightings_taxonomy():
    from zephyr_reviewer.review.pipeline import _stage_prompt

    text = _stage_prompt("security")
    for must in (
        "Trust boundaries",
        "net_buf_pull",
        "buf->len >= sizeof",
        "ARRAY_SIZE",
        "uint8_t n = payload_len",
        "1U << n",
        "ROUND_UP",
        "strncpy(dst, src, sizeof(dst))",
        "rec_len",
        "CONTAINER_OF",
        "constant-time",
        "sys_csrand_get",
        "K_SYSCALL_MEMORY_READ/WRITE",
        "K_SYSCALL_OBJ_NEVER_INIT",
        "K_OBJ_ANY",
        "sibling sites",
        "FCS-good",
        "Not a boundary",
        "speculative",
    ):
        assert must in text, must
    guide = _stage_prompt("verification")  # the guide is appended at run time; check the guide file directly
    from zephyr_reviewer.review.prompts import load_guides

    assert "Bounds enforced elsewhere" in load_guides() and "strictly more privileged" in load_guides()
    assert guide  # verification prompt still loads


def test_coding_guidelines_stage_prompt_covers_the_misra_subset_and_project_rules():
    from zephyr_reviewer.review.pipeline import _stage_prompt
    from zephyr_reviewer.review.schema import CATEGORIES

    text = _stage_prompt("coding_guidelines")
    assert text.startswith("# Stage: coding guidelines")
    for must in (
        "MISRA C:2012",
        "checkpatch",
        "U` suffix",
        "1 << 31",
        "if (ptr != NULL)",
        "else if",
        "default",
        "__fallthrough",
        "ARG_UNUSED",
        "#if defined(CONFIG_X)",
        "include guards",
        "atoi",
        "memcmp",
        "A.1 Conditional compilation",
        "A.2 Inclusive language",
        "A.3 Macro name collisions",
        "A.4 libc in the kernel",
        "A.5 libc in the codebase",
        "strtok_r",
        "`low` or `suggestion`",
        "Group violations of the same rule",
    ):
        assert must in text, must
    assert "coding_guidelines" in CATEGORIES


def test_concerns_with_the_same_file_and_title_merge_and_count_their_drafters():
    concerns: list[Concern] = []
    a = merge_concern(
        concerns, _finding("Early return leaks the bus lock", file="a.c", line=3, evidence="e1"), "logic@m1"
    )
    b = merge_concern(
        concerns, _finding("early return leaks the bus lock!", file="a.c", line=9, evidence="e2"), "concurrency@m2"
    )
    c = merge_concern(concerns, _finding("Early return leaks the bus lock", file="b.c", line=3), "logic@m1")
    assert a is b and c is not a and [x.id for x in concerns] == ["C1", "C2"]
    assert a.raised_by == ["logic@m1", "concurrency@m2"] and a.stage == "logic" and a.drafters == {"m1", "m2"}
    assert a.data["line"] == 3 and a.data["evidence"] == "e1 | e2"  # the first report's text, both evidences
    assert agreement_line(a, ["m1", "m2"]).startswith("Raised by 2 of 2 draft models (m1, m2)")
    assert agreement_line(c, ["m1", "m2"]).startswith("Raised by 1 of 2 draft models (m1)")
    assert agreement_line(a, ["m1"]) == ""  # no ensemble, no agreement talk
    msg = verification_message("open", concerns, [], ["logic", "concurrency"], ["m1", "m2"])
    assert "ran independently on 2 draft models (m1, m2)" in msg and "Raised by 1 of 2" in msg
    assert "draft models" not in verification_message("open", concerns, [], ["logic"], ["m1"])


class EnsembleRouter(StageRouter):
    """Specialists answer as StageRouter does; verification accounts for whatever ids it is shown: C* become
    findings, D* are dismissed, X* (external claims) go to hard cases, where StageRouter confirms the one that
    mentions I2C_BUS_RECOVERY and refutes the rest."""

    def handler(self, request):
        body = json.loads(request.content)
        tools = {t["function"]["name"] for t in body["tools"]}
        if "submit_verification" not in tools:
            return super().handler(request)
        self.requests.append(body)
        text = body["messages"][1]["content"]
        heads = re.findall(r"^### (C\d+) \[[^\]]*\] (.*?)  \(([^;:]*):", text, flags=re.M)
        dids = re.findall(r"^### (D\d+) ", text, flags=re.M)
        xids = re.findall(r"^### (X\d+) ", text, flags=re.M)
        out = {
            "findings": [
                {**_finding(title, file=file, line=1, severity="low", category="bug"), "source_ids": [cid]}
                for cid, title, file in heads
            ],
            "dismissed": [{"source_ids": dids, "reason": "all cite code"}] if dids else [],
            "hard_cases": [
                {"source_ids": [x], "question": "is the supplied claim real?", "severity_hint": "medium"} for x in xids
            ],
        }
        return httpx.Response(
            200, json=_completion({"content": "", "tool_calls": [_tc("v", "submit_verification", out)]})
        )


async def test_pipeline_ensemble_runs_each_specialist_per_model_and_merges(mini_repo):
    router = EnsembleRouter(verification_first_attempt_bad=False)
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(0, "t", "", "", "main", mini_repo["base"], "pr", mini_repo["head"], "", False)
    opening = opening_message(pr, intel.overview(), intel.diff_text, "", repo="o/r")
    llms = []
    for name in ("cheap/model", "second/model", "strong/model"):
        llm = _llm(ScriptedOpenRouter([]), models=(name,))
        llm._client = httpx.Client(transport=httpx.MockTransport(router.handler))
        llms.append(llm)
    cheap, second, strong = llms
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_pipeline(
            cheap,
            strong,
            bridge,
            system_prompt(load_profile("zephyr"), "English"),
            opening,
            {f.path: f.new_side_lines for f in intel.files},
            changed_files=[f.path for f in intel.files],
            added_text="",
            specialists=["intent", "concurrency", "build_config"],
            ensemble=[second],
            log=lambda m: None,
        )
    assert run.review is not None and run.errors == []
    rm = run.review_map
    assert rm["drafters"] == ["cheap/model", "second/model"]
    spec = [r for r in router.requests if "submit_concerns" in {t["function"]["name"] for t in r["tools"]}]
    assert sorted(r["model"] for r in spec) == ["cheap/model"] * 3 + ["second/model"] * 3
    # Both models raised the same three concerns: the verifier saw three merged candidates, each raised by both,
    # while dismissals are not merged (a dismissal by one model is that model's evidence).
    assert [c["raised_by"] for c in rm["concerns"]] == [
        ["intent@cheap/model", "intent@second/model"],
        ["concurrency@cheap/model", "concurrency@second/model"],
        ["build_config@cheap/model", "build_config@second/model"],
    ]
    vreq = [r for r in router.requests if "submit_verification" in {t["function"]["name"] for t in r["tools"]}][0]
    text = vreq["messages"][1]["content"]
    assert "ran independently on 2 draft models (cheap/model, second/model)" in text
    assert text.count("Raised by 2 of 2 draft models (cheap/model, second/model)") == 3
    assert "## Concerns (3)" in text and "## Dismissed concerns (4)" in text
    # Stage records keep the stage name and say which drafter ran it; the verification saw all four dismissals.
    drafted = sorted((r["stage"], r["drafter"]) for r in run.stages if "drafter" in r)
    assert drafted == [
        ("build_config", "cheap/model"),
        ("build_config", "second/model"),
        ("concurrency", "cheap/model"),
        ("concurrency", "second/model"),
        ("intent", "cheap/model"),
        ("intent", "second/model"),
    ]
    assert {d["stage"] for d in rm["dismissed_concerns"]} == {
        "concurrency@cheap/model",
        "concurrency@second/model",
        "build_config@cheap/model",
        "build_config@second/model",
    }
    assert [f.title for f in run.review.findings] == [
        "Commit message does not mention the Kconfig symbol",
        "Early return leaks the bus lock",
        "select of undefined I2C_BUS_RECOVERY",
    ]
    assert all(c["fate"] == "confirmed" for c in rm["concerns"])


def test_verification_must_account_for_external_claim_ids():
    parse = make_verification_parser({"C1", "X1"}, {"D1"})
    out, errors = parse(
        {
            "findings": [{**_finding("t"), "source_ids": ["C1"]}],
            "dismissed": [{"source_ids": ["D1"], "reason": "r"}],
            "hard_cases": [],
        }
    )
    assert out is None and any("missing: X1" in e for e in errors)
    out, errors = parse(
        {
            "findings": [{**_finding("t"), "source_ids": ["C1", "X1"]}],
            "dismissed": [{"source_ids": ["D1"], "reason": "r"}],
            "hard_cases": [],
        }
    )
    assert out is not None and errors == [] and out.findings[0]["source_ids"] == ["C1", "X1"]
    out, errors = parse(
        {
            "findings": [],
            "dismissed": [{"source_ids": ["X1", "D1"], "reason": "r"}],
            "hard_cases": [{"source_ids": ["C1"], "question": "q", "severity_hint": "low"}],
        }
    )
    assert out is None and any("X1 is not allowed in dismissed" in e for e in errors)


async def test_pipeline_verifies_external_claims_and_reports_a_verdict_for_each(mini_repo):
    router = EnsembleRouter(verification_first_attempt_bad=False)
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(0, "t", "", "", "main", mini_repo["base"], "pr", mini_repo["head"], "", False)
    opening = opening_message(pr, intel.overview(), intel.diff_text, "", repo="o/r")
    cheap = _llm(ScriptedOpenRouter([]), models=("cheap/model",))
    strong = _llm(ScriptedOpenRouter([]), models=("strong/model",))
    for llm in (cheap, strong):
        llm._client = httpx.Client(transport=httpx.MockTransport(router.handler))
    claims = [
        Claim(
            "X1",
            "drivers/i2c/Kconfig:20: I2C_BUS_RECOVERY is selected but never defined",
            "drivers/i2c/Kconfig",
            20,
            "notes.txt",
        ),
        Claim("X2", "drivers/i2c/i2c_foo.c:30: the semaphore count is wrong", "drivers/i2c/i2c_foo.c", 30, "notes.txt"),
    ]
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_pipeline(
            cheap,
            strong,
            bridge,
            system_prompt(load_profile("zephyr"), "English"),
            opening,
            {f.path: f.new_side_lines for f in intel.files},
            changed_files=[f.path for f in intel.files],
            added_text="",
            specialists=["intent"],
            claims=claims,
            log=lambda m: None,
        )
    assert run.review is not None and run.errors == []
    vreq = [r for r in router.requests if "submit_verification" in {t["function"]["name"] for t in r["tools"]}][0]
    text = vreq["messages"][1]["content"]
    assert "## External claims (2)" in text and "### X1 [medium, speculative]" in text and "from external" in text
    assert "Being supplied is not evidence." in text
    verdicts = {c.id: c for c in run.review.external_claims}
    assert verdicts["X1"].verdict == "confirmed" and verdicts["X1"].finding_id.startswith("HF")
    assert verdicts["X2"].verdict == "refuted" and "second commit message" in verdicts["X2"].reason
    assert (
        verdicts["X1"].title == "I2C_BUS_RECOVERY is selected but never defined"
        and verdicts["X1"].source == "notes.txt"
    )
    titles = [f.title for f in run.review.findings]
    assert "select of undefined I2C_BUS_RECOVERY" in titles  # the hard case's confirmed finding
    xmap = {c["id"]: c for c in run.review_map["external_claims"]}
    assert xmap["X1"]["fate"] == "confirmed" and xmap["X2"]["fate"] == "refuted"
    rreq = [r for r in router.requests if "submit_report" in {t["function"]["name"] for t in r["tools"]}][0]
    assert "External claims checked: 1 confirmed, 1 refuted, 0 undecided" in rreq["messages"][1]["content"]


def test_batched_hard_cases_need_one_verdict_each():
    from zephyr_reviewer.review.pipeline import make_hard_cases_parser

    parse = make_hard_cases_parser(["H2", "H5"])
    out, errs = parse({"verdicts": [{"case": "H2", "verdict": "refuted", "reason": "the guard exists"}]})
    assert out is None and errs == ["every case needs a verdict; missing: H5"]
    out, errs = parse(
        {
            "verdicts": [
                {"case": "H2", "verdict": "refuted", "reason": "the guard exists"},
                {"case": "H2", "verdict": "refuted", "reason": "again"},
                {"case": "H9", "verdict": "refuted", "reason": "?"},
                {"case": "H5", "verdict": "confirmed", "reason": "no finding given"},
            ]
        }
    )
    assert out is None
    assert "verdicts[1]: H2 is decided twice" in errs and any("unknown case 'H9'" in e for e in errs)
    assert any(e.startswith("H5: a confirmed verdict needs a complete finding") for e in errs)
    out, errs = parse(
        {
            "verdicts": [
                {"case": "H5", "verdict": "confirmed", "reason": "seen", "finding": _finding("t")},
                {"case": "H2", "verdict": "refuted", "reason": "the guard exists"},
            ]
        }
    )
    assert errs == [] and [v["case"] for v in out["verdicts"]] == ["H5", "H2"]
    assert parse({"verdicts": "no"}) == (None, ["arguments must be an object with a verdicts list"])


def test_verification_gives_every_external_claim_its_own_hard_case():
    parse = make_verification_parser({"C1", "X1", "X2"}, set())
    out, errs = parse(
        {
            "findings": [],
            "dismissed": [],
            "hard_cases": [{"source_ids": ["X1", "X2", "C1"], "question": "q", "severity_hint": "low"}],
        }
    )
    assert out is None and any(e.startswith("hard_cases[0] groups the external claims X1, X2") for e in errs)
    out, errs = parse(
        {
            "findings": [],
            "dismissed": [],
            "hard_cases": [
                {"source_ids": ["X1", "C1"], "question": "q", "severity_hint": "low"},
                {"source_ids": ["X2"], "question": "q", "severity_hint": "low"},
            ],
        }
    )
    assert errs == [] and [h.source_ids for h in out.hard_cases] == [["X1", "C1"], ["X2"]]


class BatchRouter(EnsembleRouter):
    """Answers a batch of hard cases with a verdict and a reason per case."""

    def handler(self, request):
        body = json.loads(request.content)
        tools = {t["function"]["name"] for t in body["tools"]}
        if "submit_hard_case_verdicts" not in tools:
            return super().handler(request)
        self.requests.append(body)
        text = body["messages"][1]["content"]
        verdicts = []
        for case_id, block in re.findall(r"^## Hard case (H\d+)\n(.*?)(?=^## Hard case |\Z)", text, flags=re.M | re.S):
            if "I2C_BUS_RECOVERY" in block:
                verdicts.append(
                    {
                        "case": case_id,
                        "verdict": "confirmed",
                        "reason": "kconfig_symbol shows no definition",
                        "finding": _finding(
                            "select of undefined I2C_BUS_RECOVERY",
                            file="drivers/i2c/Kconfig",
                            line=20,
                            severity="medium",
                            category="build_config",
                        ),
                    }
                )
            else:
                where = re.search(r"\((\S+?):\d+", block)
                verdicts.append(
                    {
                        "case": case_id,
                        "verdict": "refuted",
                        "reason": f"nothing wrong at {where.group(1) if where else '?'}",
                    }
                )
        calls = [_tc("hb", "submit_hard_case_verdicts", {"verdicts": verdicts})]
        return httpx.Response(200, json=_completion({"content": "", "tool_calls": calls}))


async def test_batched_hard_cases_keep_their_own_reasons(mini_repo):
    router = BatchRouter(verification_first_attempt_bad=False)
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(0, "t", "", "", "main", mini_repo["base"], "pr", mini_repo["head"], "", False)
    opening = opening_message(pr, intel.overview(), intel.diff_text, "", repo="o/r")
    cheap = _llm(ScriptedOpenRouter([]), models=("cheap/model",))
    strong = _llm(ScriptedOpenRouter([]), models=("strong/model",))
    for llm in (cheap, strong):
        llm._client = httpx.Client(transport=httpx.MockTransport(router.handler))
    claims = [
        Claim("X1", "drivers/i2c/Kconfig:20: I2C_BUS_RECOVERY is never defined", "drivers/i2c/Kconfig", 20, "e"),
        Claim("X2", "drivers/i2c/i2c_foo.c:30: the semaphore count is wrong", "drivers/i2c/i2c_foo.c", 30, "e"),
        Claim("X3", "drivers/i2c/i2c_foo.c:12: the name is misspelled", "drivers/i2c/i2c_foo.c", 12, "e"),
    ]
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_pipeline(
            cheap,
            strong,
            bridge,
            system_prompt(load_profile("zephyr"), "English"),
            opening,
            {f.path: f.new_side_lines for f in intel.files},
            changed_files=[f.path for f in intel.files],
            added_text="",
            specialists=["intent"],
            claims=claims,
            max_hard_cases=1,  # everything lands in one batch
            log=lambda m: None,
        )
    assert run.review is not None and run.errors == []
    batch = [r for r in router.requests if "submit_hard_case_verdicts" in {t["function"]["name"] for t in r["tools"]}]
    assert len(batch) == 1
    text = batch[0]["messages"][1]["content"]
    assert "# 3 hard cases, each decided on its own" in text and text.count("## Hard case H") == 3
    assert "a reason describes that one case and never refers to the others" in text
    verdicts = {c.id: c for c in run.review.external_claims}
    assert verdicts["X1"].verdict == "confirmed" and verdicts["X1"].finding_id.startswith("HF")
    assert verdicts["X2"].verdict == "refuted" and verdicts["X2"].reason == "nothing wrong at drivers/i2c/i2c_foo.c"
    assert verdicts["X3"].verdict == "refuted" and verdicts["X3"].reason == "nothing wrong at drivers/i2c/i2c_foo.c"
    stage = [s for s in run.stages if s["stage"] == "H1+H2+H3"][0]
    assert stage["verdict"] == "H1 confirmed, H2 refuted, H3 refuted"
