# zephyr-reviewer

Source intelligence for the Zephyr tree, exposed over [MCP](https://modelcontextprotocol.io), and
an independent review agent that drives it through [OpenRouter](https://openrouter.ai).

A diff-only reviewer guesses. This one looks things up. The model reviewing a pull request can read
whole functions at the head commit, find a symbol's definition and callers, read the Kconfig block a
`select` points at, open the devicetree binding a driver claims to implement, see which twister
suites cover the area, and find out who maintains it. It then submits a structured review that is
validated against the diff and posted back to the pull request.

```
GitHub Actions runner (one job per pull request)
┌───────────────────────────────────────────────────────────────────────────┐
│  zephyr-reviewer review                                                   │
│  ┌──────────────────────┐   OpenAI-style tool calls   ┌─────────────────┐ │
│  │  review agent        │ ◀──────────────────────────▶│   OpenRouter    │ │
│  │  (orchestrator)      │                             │ model routing   │ │
│  └──────────┬───────────┘                             └─────────────────┘ │
│             │ MCP over stdio                                              │
│  ┌──────────▼───────────┐   git plumbing, pinned to the head commit       │
│  │  zephyr-source-intel │ ─────────────────────────▶  Zephyr checkout     │
│  │  MCP server          │   nothing in the tree is executed               │
│  └──────────────────────┘                                                 │
│             │ findings ─▶ GitHub PR comment / inline review / artifacts   │
└───────────────────────────────────────────────────────────────────────────┘
```

Nothing is deployed. Each workflow run starts its own MCP server, uses it to gather context,
reviews, publishes, and exits. The MCP tool surface is the contract; when indexing, review history
and project knowledge move into a shared service, the agent and the workflow stay the same and the
server behind the tools changes.

## Status

Phase 1, working end to end against a local checkout and scripted services; not yet run against a
live pull request with a paid model. See [What is not done](#what-is-not-done).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

Explore the tool surface against a real Zephyr checkout:

```bash
cd ~/zephyrproject/zephyr
BASE=$(git merge-base HEAD upstream/main)

zephyr-reviewer tools --base "$BASE"
zephyr-reviewer call  --base "$BASE" find_definition name=k_sem_take
zephyr-reviewer call  --base "$BASE" kconfig_symbol  name=CONFIG_I2C_RTIO
zephyr-reviewer call  --base "$BASE" dt_binding      compatible_or_name=nordic,nrf-twim
zephyr-reviewer call  --base "$BASE" maintainers     path=drivers/i2c/i2c_nrfx_twim.c
```

Review a local branch without touching GitHub:

```bash
export OPENROUTER_API_KEY=sk-or-...
zephyr-reviewer review --repo ~/zephyrproject/zephyr --base "$BASE" --head HEAD --dry-run
```

Review a real pull request, posting nothing (artifacts land in `review-output/`):

```bash
export GITHUB_TOKEN=$(gh auth token)
zephyr-reviewer review --repo ~/zephyrproject/zephyr --repository zephyrproject-rtos/zephyr --pr 12345 --dry-run
```

Drop `--dry-run` to post. Anchored findings go inline by default; `--no-inline` puts everything in
the top-level comment. `--ask` shows the finished review, the summary comment and every inline comment with its file and
line range, and prompts before anything is posted. `e` edits `comment.md`, `i` edits
`inline_comments.json` (to drop or reword one), and the prompt returns with the edited versions:

```
Post the summary comment and 2 inline comment(s) to PR #12345? [y]es / [n]o / [e]dit comment.md / [i] edit inline_comments.json:
```

### Publish modes

`publish` is `post`, `ask` or `never` (`dry_run: "true"` means `never`). In a job with no terminal,
`ask` saves the review without posting and prints the command to publish it after a person has
read it:

```bash
zephyr-reviewer post --output-dir review-output --repository zephyrproject-rtos/zephyr [--inline]
```

`post` reads `comment.md` and `inline_comments.json` from the directory, so both can be edited or
trimmed first, asks for confirmation (or `--yes`), and publishes with the same hidden marker so a
later run updates it in place. This is the shape Zephyr's AI policy asks for: a human reads the
model's output before it reaches the pull request. Like the other safety settings, `publish` can
be tightened by flags but not relaxed below the workflow's value.

### Use the MCP server from another client

The server is a normal MCP server. Point Claude Code, an IDE, or any MCP client at it to get the
same lookups while reviewing by hand:

```bash
zephyr-reviewer serve --repo ~/zephyrproject/zephyr --base upstream/main --head HEAD
# or as a long-lived HTTP endpoint, the shape the shared service will take:
zephyr-reviewer serve --repo ~/zephyrproject/zephyr --base upstream/main --transport streamable-http --port 8765
```

## The tools

| Tool | Answers |
| --- | --- |
| `pr_overview` | Commits, changed files with +/- counts, MAINTAINERS area per file |
| `commit_messages` | Full commit messages, oldest first |
| `file_diff` | Diff of one changed file with chosen context |
| `read_file` / `read_file_at_base` | Numbered source at head (added lines marked `+`) or at base |
| `search` / `list_files` | `git grep` (POSIX ERE) and glob listing over the head tree |
| `find_definition` | Where a C function, macro, struct, typedef, Kconfig symbol, DT compatible or CMake function is defined; resolves `k_foo()` to `z_impl_k_foo()` |
| `find_references` | Callers and users, counted per area, changed files first |
| `kconfig_symbol` | Every definition block of a symbol and its users in Kconfig, `.conf`, CMake and source |
| `dt_binding` | Binding YAML for a compatible, the drivers that bind to it, the nodes that use it |
| `maintainers` | MAINTAINERS.yml areas, with get_maintainer.py's matching rules, read from the trusted base commit |
| `related_tests` | twister suites (tests.yaml / sample.yaml) covering a path or mentioning a symbol |
| `test_candidates` / `list_platforms` | Suites, scenarios, tags and platforms implicated by the change; valid platform identifiers |
| `file_history` / `blame` | Recent commits and blame for a range |
| `project_guidelines` | AGENTS.md and the doc/contribute pages by topic |

Every result is text, bounded in size, and says when it was truncated. Every query runs against an
explicit commit through `git show`, `git grep` and `git ls-tree`, never the working tree, and
nothing from the tree is executed. That is what makes it safe to run on a fork's head under
`pull_request_target`.

## The review loop

1. The orchestrator resolves the pull request (head SHA, merge base via the compare API), fetches
   both commits by SHA if the checkout lacks them, and starts the MCP server over stdio.
2. The system prompt is the Zephyr review profile (`src/zephyr_reviewer/review/profiles/zephyr.md`)
   plus working rules. The opening message carries the PR description, the overview, and the full
   diff when it fits.
3. The model investigates with the tools. Each turn may issue several calls; results go back with
   their `tool_call_id`. A step budget and a prompt-size budget bound the cost; when either runs out
   the model is forced to submit.
4. The existing discussion (comments, inline threads, review states) is digested into the opening
   message, with a `pr_discussion` tool for the full text, so the model builds on the review rather
   than restarting it.
5. The model ends by calling `submit_review`, a client-side tool whose arguments are the review.
   Findings are validated: a `line` must be on the new side of the diff to become an inline
   comment, otherwise it is demoted to the summary with a note; a file outside the diff is demoted
   too; a malformed submission is rejected with reasons and the model retries.
6. Findings anchored to a changed line are posted as inline review comments at that line, each
   self-contained with problem, trigger, impact, recommendation and a committable suggestion. The
   top-level comment (one per PR, updated in place through a hidden marker) carries the verdict,
   summary, test gaps, commit-message and discussion issues, the test plan, and any finding that
   could not be anchored. A re-run skips inline comments it already posted for the same finding at
   the same location, so threads stay single. If GitHub rejects the inline review, the findings
   are folded back into the top-level comment. `post_inline_comments: "false"` puts everything in
   the top-level comment. `review-output/` receives `comment.md`, `inline_comments.json`,
   `review.json` and `transcript.jsonl`.

### Test plan (option)

With `generate_test_plan: "true"` (or `--test-plan`), the review also proposes the twister runs
that would exercise the change, with platform coverage that matches what changed. The model works
from a `test_candidates` tool that reimplements the signals Zephyr's own `scripts/ci/test_plan.py`
uses, without executing anything from the tree: suites the PR edits (including the `common`
directory rule), MAINTAINERS `tests:` identifiers, tags from `scripts/ci/tags.yaml`, suites
covering the changed source, and the platforms implicated by changed boards (via board.yml and
DTS file names), SoCs, architectures and the boards whose devicetree instantiates a changed
driver's compatible. A `list_platforms` tool gives valid platform identifiers.

Each entry names a suite, optionally one scenario, platforms, a priority and what the run
verifies. Before publishing, the plan is checked against the tree: unknown suites, misspelled
scenarios (with the closest match suggested), unknown platform identifiers, and platforms a suite's
`platform_allow` or `platform_exclude` rules out are flagged in the comment. The comment carries a
table and ready-to-run `west twister` commands; `review-output/test_plan.yaml` holds the same plan
for automation.

### Specialist pipeline (advanced)

`pipeline: multi-stage` replaces the single agent with a pipeline modelled on
[sashiko](https://github.com/sashiko-dev/sashiko):

1. **Plan**, deterministically: `intent` and `logic` always run; `error_handling` (unchecked return
   codes, cleanup on error paths, assertions used where runtime checks are needed) and
   `coding_guidelines` (Zephyr's MISRA C:2012 subset and project rules A.1 to A.5, the ones
   checkpatch does not cover) run whenever C code changes; `concurrency`, `resources`, `hardware`,
   `build_config` and `security` are added from the files and code the change touches (or set
   `specialists` explicitly).
2. **Specialists**, in parallel, each with the tools, a focused prompt and its own step budget. Each
   returns `concerns` and `dismissed` concerns with evidence; dismissals matter because they stop
   another pass's identical guess from surviving, and weak ones are audited.
3. **Verification** on the primary model, with a Zephyr false-positive guide and severity
   calibration. Every concern `C*` and dismissal `D*` must land in exactly one of `findings`,
   `dismissed` or `hard_cases`, or the submission is rejected with the missing ids. Concerns can be
   confirmed or sent to hard cases, never dropped here; a dismissal can be accepted only when it
   cites disproving code.
4. **Hard cases**, in parallel, one per contested or speculative candidate up to `max_hard_cases`
   (the rest share one run but are still decided one by one, each with its own reason), each with
   tools and its own budget, confirming with a complete finding or
   refuting with evidence. An undecided case is kept as speculative rather than lost.
5. **Report**, without tools: summary, verdict and the issue-only assessments; the findings are
   fixed, and the verdict is forced to `request_changes` when a high or critical finding stands.

Findings whose trigger path rests on an unverified assumption carry `speculative: true` and are
capped at medium severity, reported rather than buried. Verification of genuinely borderline
candidates is where runs on the same change can differ; `show_refuted_candidates: "true"` (or
`--show-refuted`) appends a collapsed one-line-per-item list of what was raised and refuted, with
the reason, so a maintainer can see those calls without them counting as findings. `review-output/review_map.json` records the
lineage: every candidate, which stage produced it, and where it ended and why. When `draft_model`
is set the specialists run on it and the primary model verifies.

#### Draft ensemble

`draft_ensemble` (or `--draft-ensemble a,b`) names additional low-cost models. Every specialist
then runs once per model, in parallel, which buys candidate diversity: two different model families
miss different things. Concerns about the same file and title are merged before verification, and
each merged concern tells the verifier how many of the models raised it. Agreement corroborates a
candidate; a concern only one model raised goes to a hard case unless its evidence already proves
it. Specialist cost scales with the number of models, verification cost only with the concerns they
disagree on. `review_map.json` records `drafters` and, per concern, `raised_by` (`stage@model`).
Measure it with the benchmark before making it a default: the variance we have seen between runs
was in verification, not in candidate generation, so an ensemble raises recall but does not by
itself make two runs agree.

### Two-stage review (advanced)

Investigation turns dominate cost: the whole transcript is resent on every call. Setting
`draft_model` makes a low-cost model do that work and a stronger one finish it:

```yaml
with:
  model: deepseek/deepseek-v4.1-flash     # verifies and finalizes
  draft_model: google/gemini-2.5-flash   # investigates and drafts
  verify_max_steps: "15"
```

Stage 1 runs the normal review with the cheap model. Stage 2 receives the same opening message plus
the draft review and a dossier of the evidence stage 1 gathered (deduplicated tool results, highest
value first, within `dossier_chars`), keeps live tool access with its own smaller step budget, and
is told to confirm, correct or drop every draft finding against the code, then spend what remains
on the riskiest regions the draft may have missed. Only the final review is published;
`review.json` and the step summary record both stages' models, steps and cost. If the cheap model fails to
submit, stage 2 runs as a full single-stage review, so the option can only cost, never degrade.

Expect the saving to be large when the draft model is an order of magnitude cheaper and the
verifier needs few steps; measure it on your own pull requests before relying on it, and watch
the verifier's drop rate: a draft model that is wrong most of the time is not saving anything.

### Existing discussion

The reviewer reads what has already been said. Before the model starts, the orchestrator fetches
the conversation, the inline review threads (grouped, with file and line, outdated ones marked),
and each reviewer's latest review state, and puts a bounded digest in the opening message. The
full text is available through a `pr_discussion` tool, filterable by file. The previous automated
review, if one exists, is shown separately and labelled as not being evidence.

The profile tells the model how to use it: a concern a maintainer already raised is theirs, not a
new finding; a request the head commit satisfies is resolved; author replies are checked against
the code rather than taken on trust. Only problems are reported, in an "Unresolved discussion"
section of the comment: a request the head commit does not address, or a claim the code
contradicts. Turn it off with `include_discussion: "false"`.

### Prefetched context

Models issue tool calls one at a time, and every call resends the whole transcript, so each lookup
the model does not have to make is the cheapest saving available. The opening message therefore
carries, read from git objects at the head commit and labelled with its SHA: the function each hunk
lands in (whole, or an excerpt around the change for very long functions), small windows around
changes outside any function, whole Kconfig, devicetree, binding and `.conf` files when they are
short, and one line per changed function naming its callers by area with example sites. Blocks are
ranked by changed lines and kept within `prefetch_chars` (default 24,000); whatever does not fit is
named so the model can fetch it with `read_file`. Set `prefetch_chars: "0"` to disable.

### Linked issues and pull requests

References in the PR title and body, the commit messages and the discussion (`#123`,
`owner/repo#123`, GitHub URLs) are resolved, up to `max_linked_issues` of them (default 8). The
opening message carries one entry per item: how it relates (fixes, closes, follow-up to, see), its
kind and status (open, closed, merged), labels, title, author with trust, and the start of its
body, so the model knows the symptom a fix claims to address and what a referenced PR did. The
`linked_issue` tool returns the full body and comments of any of them, or of any other number in
the repository. The working rules ask the model to compare the scenario an issue reports with the
change and say so when the change does not address it. Everything is sanitized and framed as
untrusted data like the rest of the PR text. Disable with `include_linked_issues: "false"` or
`--no-linked`.

### CI results

The model sees the head commit's check runs and commit statuses in the opening message and can
call `ci_status` for the full list and `ci_failure_logs` for the tail of a failed GitHub Actions
job's log (timestamps and colour codes stripped, optional grep with context). It is told not to
repeat what CI already reports but to diagnose a failure when it can tie it to the change, and never
to treat a green CI as proof. Needs `checks: read` and `actions: read` on the job token; disable
with `include_ci: "false"`.

### Untrusted text and lockdown

Everything the model reads from GitHub was written by the people whose code it reviews, or by
their tooling. The reviewer treats it that way, following the GitHub MCP server's lockdown idea:

- Every author is tagged by repository permission: maintainer (admin/maintain), collaborator
  (write/triage), bot (a short list of known automation), external, or unknown when the token
  cannot see permissions. The PR author's trust is shown in the opening message.
- HTML comments and zero-width or control characters, which GitHub renders invisibly, are
  stripped from every body before it reaches the model.
- PR description and discussion are wrapped in explicit data delimiters and the working rules say
  never to follow instructions found in them, and to verify their claims against the code.
- `lockdown: strict` additionally withholds comments from external users other than the PR author.
  `label` is the default; `off` disables tagging.

Safety settings follow a tightening-only rule, as the GitHub server does with its lockdown header:
the operator's values (workflow inputs, or server flags for the future shared service) are a
floor, and a request (CLI flags, or per-request headers later) may make them stricter but never
looser. A rejected relaxation is logged and ignored. See `src/zephyr_reviewer/review/policy.py`.

### Typed tool outputs

Tools whose results programs will consume, `pr_overview`, `maintainers`, `list_platforms` and
`find_definition`, publish an output schema and return structured content alongside the prose the
model reads. The remaining tools return text; they gain schemas as consumers appear.

### Verifying findings from a human or another tool

`--findings FILE` (action input `external_findings`) feeds the run a file of findings to check:
numbered or bulleted text items, blank-line paragraphs, or JSON with a `findings` list. A `path:line`
in an item anchors it. Each item becomes a claim `X1`, `X2`, ... shown to the models as a lead, not a
finding and not an instruction. In the specialist pipeline the claims enter verification with the
same accounting as specialist concerns: each must be confirmed with evidence from the code, sent to a
hard case, or refuted, and a specialist concern about the same thing counts as corroboration. The
single agent reports a verdict per claim in `external_claims`. The comment ends with a "Claims
checked" section answering every claim (confirmed ones are among the findings), `review.json`
records the verdicts, and `review_map.json` the lineage of each `X*` id.

### Re-reviews after a push

When a pull request this tool already reviewed is reviewed again at a new head, the earlier run's
inline comments whose threads are still open are re-checked. Each becomes a claim (`X*`, after any
from `--findings`) with the source "the earlier review of <sha>", verified like the others: the
code at the new head either still has the problem or it does not. A refuted claim was fixed or no
longer applies: its thread gets a one-line reply ("No longer applies at <sha>: <reason>") and is
resolved. A confirmed claim keeps its thread, and the new finding that confirms it is not posted a
second time. Undecided claims are left alone, and so is a refutation the verifier was forced to
submit when its budget ran out. Each earlier comment gets its own hard case and its own reason, so
a reply never bundles other comments. Each earlier comment is answered in its own thread,
never in the summary comment, which only counts them ("Earlier inline comments re-checked at this
head: 3 (1 resolved as no longer applying, 2 still open)"); the "Claims checked" section is for
claims from `--findings` only. Nothing happens on a run at the same head, in a dry
run, or in `ask` mode until the review is posted (`zephyr-reviewer post` resolves from the saved
`claims.json`). The step's `resolved_count` output says how many threads were resolved; set
`resolve_addressed: "false"` (or `--no-resolve-addressed`) to leave earlier comments alone.

Resolving a thread is the `resolveReviewThread` GraphQL mutation, which GitHub only allows a
token that has `contents: write`, even though it changes no content; a workflow token with
`pull-requests: write` alone is refused ("Resource not accessible by integration"), while the
reply itself is accepted. With such a token the answer is posted and the thread stays open for a
person to resolve; the reply carries a hidden marker so a later run does not answer the same
thread again, and only retries the resolution. To have threads resolved, give the job
`contents: write` or pass a token that has it as `github_token`. The log says what the run
decided about every earlier comment either way, and names the missing permission when a
resolution is refused.

### Log verbosity

The log records every model call and tool call by default, prefixed with the stage and step. `--quiet`
(`-q`, or `quiet: "true"` in the action) keeps only the major steps: what is being reviewed, each stage
starting and finishing with its result and step count, verification and hard-case outcomes, the
artifacts written and what was posted. Errors and model retries are logged either way, and
`review.json` keeps the full transcript regardless.

### Run details

The posted comment ends with the one-line advisory notice and nothing else. Models, tokens and cost
appear nowhere by default, not in the comment, the job summary or the log; only `review.json` in the
artifacts records them. `--show-details` (`show_run_details: "true"`) prints them in the local log
for testing and debugging without changing what is posted. `--post-details`
(`post_run_details: "true"`) is the separate switch that appends them to the posted comment and the
job summary.

### False-positive guide and severity calibration

Two prompts, [false_positive_guide.md](src/zephyr_reviewer/review/profiles/false_positive_guide.md)
and [severity.md](src/zephyr_reviewer/review/profiles/severity.md), are applied wherever a finding
is submitted or confirmed: appended to the single agent's prompt under "Before you submit", given
to the verifier in two-stage mode, and to the verification and hard-case stages of the specialist
pipeline (never to specialists, whose job is to raise candidates, and never to the report stage,
whose findings are fixed). They are operational rather than advisory, following sashiko: no
defensive checks without an untrusted source or a concrete bad caller; unchecked returns only
when the call can fail and the API contract makes the check the caller's job; Kconfig-guarded code
judged per configuration; binding-required devicetree properties are present; ISR-legal kernel
calls named; pre-existing defects are not this PR's findings; commit-message claims are claims.

Severity is reasoned from consequence, then trigger path, then reachability, and reachability
never lowers a level. A finding whose trigger path rests on an unverified assumption is marked
`speculative`, reported with severity capped at medium, and never dropped for being "probably
unreachable". Edit the two files to tune the reviewer's judgement without touching code.

### Budgets, timeouts and what you are waiting for

Every model request is announced in the log with the model, message count and approximate prompt
size, and answered with its duration and the number of tool calls it made. While a model request
or a tool call is outstanding a heartbeat line appears every 30 seconds naming what is being waited
for, so a slow provider or a stuck lookup is visible as such. The MCP server runs its lookups in
worker threads, so one slow call never holds up the other stages' calls behind it. Retries after transient errors or timeouts
are logged with the reason and the wait. Four limits bound a run:

| Setting | Default | Effect |
| --- | --- | --- |
| `max_steps` | 40 | Model turns before the review is forced to submit |
| `max_context_tokens` | 160000 | Prompt size at which it is forced to submit |
| `model_timeout_seconds` | 300 | Wait for one model response before retrying |
| `tool_timeout_seconds` | 120 | Wait for one tool call; a hung lookup becomes an error the model sees and works around |
| `max_stage_seconds` | 0 (off) | Wall-clock budget per agent run (each stage in a staged pipeline); forces submission |

A forced submission is marked in the log and in the run details, and a review cut short should be
read with that in mind.

### What reaches the pull request

Every text the reviewer posts, the summary, the inline comments, the thread replies, is passed
through the same two filters: the run's internal ids (`C*`, `D*`, `H*`, `X*`) are removed, and
every `@word` outside code is wrapped in backticks, so a Doxygen `@since` or a name copied from
the discussion never notifies anyone. The reviewer does not mention people.

### Issues only

The review reports problems and nothing else. The summary states the result and the key issue
rather than describing the change. The test, commit-message and discussion assessments appear in
the comment only when something is missing or wrong; the schema and profile say so, and a filter
drops short all-clear boilerplate ("tests are adequate", "no issues found") should a model write it
anyway. A clean pull request gets a short approval.

### Model selection

OpenRouter does the routing. `model` is the primary slug and `fallback_models` are tried in order
by OpenRouter itself (the request carries the `models` array). `provider` passes
[provider routing preferences](https://openrouter.ai/docs/features/provider-routing) through
unchanged, so you can require `{"data_collection": "deny"}` or pin an ordering. Transient failures
retry with backoff and honor `Retry-After`.

## GitHub Actions

`action.yml` is a composite action. [examples/zephyr-ai-review.yml](examples/zephyr-ai-review.yml)
is a workflow to drop into the Zephyr repository or a fork. Points that matter:

- It uses `pull_request_target` so forked pull requests can reach the OpenRouter secret. That is
  safe only because the workflow checks out the base branch, the action fetches the head by SHA,
  and nothing from the pull request is executed. Do not add a head checkout or a build step to
  that job.
- A shallow checkout is enough; the action fetches missing commits itself. Depth only improves
  `file_history` and `blame`.
- `skip_reviewed: same-head` (or `--skip-reviewed same-head`) skips a PR whose current head this tool
  already reviewed, so re-running a workflow or sweeping a list of PRs does not pay twice; `any`
  skips a PR with any earlier review by this tool. The summary comment carries a hidden
  `<!-- tag:head:sha -->` marker for this. The default, `never`, reviews again and updates the comment.
- A review at a new head re-checks the earlier run's open inline comments and resolves the ones
  that no longer apply (see "Re-reviews after a push").
- The example gates on an `ai-review` label so cost is opt-in. Remove `required_labels` to review
  everything.
- `fail_on_severity` turns the step into a gate. Leave it at `none` until the false-positive rate
  is known.

Inputs and outputs are documented in [action.yml](action.yml).

### Zephyr's AI policy

Zephyr's contributor guidelines ask that review comments written with AI help be verified by a
human before posting. Every comment this tool posts is labelled as model-generated and advisory,
and `dry_run` plus the uploaded artifacts let a maintainer read the review before anything reaches
the pull request. Decide with the project how it should be used before enabling posting on the
upstream repository.

## Benchmarking against history

Every change to the profile, the pipeline or the model choice should have a number. The benchmark
reviews commits that later turned out to be buggy, as if they were new, and grades the findings
against the fix that followed. The idea and the dataset shape follow
[sashiko](https://github.com/sashiko-dev/sashiko).

```bash
# Build a dataset from fix commits. Fixes: <sha> trailers are exact; for fixes that only reference
# an issue, the lines the fix removes are blamed at its parent and the commit that introduced most
# of them is the culprit, with that share recorded as confidence.
zephyr-reviewer benchmark mine --repo ~/zephyrproject/zephyr --range upstream/main --since 2025-06-01 \
    --max 150 --out benchmarks/zephyr-fixes.json

# Review every culprit against its parent, judge each result, and report.
export OPENROUTER_API_KEY=sk-or-...
zephyr-reviewer benchmark run benchmarks/zephyr-fixes.json --repo ~/zephyrproject/zephyr \
    --limit 20 --concurrency 3 --judge-model deepseek/deepseek-v4.1-flash

# Re-grade an existing run (new judge model or prompt) without reviewing again.
zephyr-reviewer benchmark run benchmarks/zephyr-fixes.json --repo ~/zephyrproject/zephyr \
    --analyze-only benchmarks/runs/<run>
```

Each entry has `commit`, `fixed_by`, `source` (`trailer`, `blame` or `manual`), `confidence`,
`subsystem` (from MAINTAINERS.yml), `files` and `problem_description` (the fix's message without
trailers). Hand-written entries need only `commit` and `problem_description`.

A run directory holds one folder per entry with the usual review artifacts plus `entry.json` and
`judgement.json`, and `results.json` and `summary.md` for the whole run: detection (strict and
lenient, by subsystem and by source), noise (findings per review, unmatched findings by
severity, verdict distribution) and resources (steps, tool calls, tokens, cost and time as
mean, p50, p90 and max). The judge is a model with a forced tool call classifying each entry as
`DETECTED`, `PARTIALLY_DETECTED` or `MISSED`, told not to reward volume. `--draft-model`,
`--max-steps` and `--profile` let you compare pipeline variants on the same dataset; filter with
`--subsystem`, `--source` and `--min-confidence`.

`benchmarks/zephyr-fixes.json` is a dataset mined from upstream since mid-2025. Expect the blame
source to carry some wrong culprits; treat `confidence` below 0.8 with care and prefer `--source
trailer` when precision matters more than size.

## Layout

| Path | Purpose |
| --- | --- |
| `src/zephyr_reviewer/intel/repo.py` | Read-only git access pinned to a commit |
| `src/zephyr_reviewer/intel/diff.py` | Unified diff parsing, new-side line sets |
| `src/zephyr_reviewer/intel/symbols.py` | Definition and reference heuristics for C, Kconfig, DT, CMake |
| `src/zephyr_reviewer/intel/kconfig.py`, `devicetree.py`, `maintainers.py`, `tests_index.py`, `platforms.py` | Domain lookups |
| `src/zephyr_reviewer/intel/service.py` | `SourceIntel`: the facade the tools are written against |
| `src/zephyr_reviewer/mcp_server.py` | The MCP server (stdio or streamable HTTP) |
| `src/zephyr_reviewer/review/mcp_client.py` | Spawns or connects to the server; tool schemas for the model |
| `src/zephyr_reviewer/review/openrouter.py` | Chat completions with tools, fallback routing, retries |
| `src/zephyr_reviewer/review/agent.py` | The loop: tool calls, budgets, `submit_review` |
| `src/zephyr_reviewer/review/schema.py` | Review schema and validation against the diff |
| `src/zephyr_reviewer/review/ci.py` | Check runs, statuses and failed-job log tails as tools |
| `src/zephyr_reviewer/review/policy.py` | Safety settings a request may tighten but never relax |
| `src/zephyr_reviewer/review/testplan.py` | Validates a proposed test plan against suites, scenarios and platforms |
| `src/zephyr_reviewer/review/stages.py` | Two-stage review: draft with a cheap model, verify with a strong one |
| `src/zephyr_reviewer/review/pipeline.py` | Specialist pipeline: planner, accounting rules, hard cases, review map |
| `src/zephyr_reviewer/review/profiles/stages/` | Stage prompts, including the verification false-positive guide |
| `src/zephyr_reviewer/review/discussion.py` | PR comments and threads with author trust and resolution state; digest and `pr_discussion` tool |
| `src/zephyr_reviewer/review/linked.py` | Issues and PRs the change refers to; digest and `linked_issue` tool |
| `src/zephyr_reviewer/review/render.py`, `publish.py`, `github_api.py` | Comment rendering and delivery |
| `src/zephyr_reviewer/benchmark/` | Dataset mining from fix history, LLM judge, runner and report |
| `src/zephyr_reviewer/cli.py` | `serve`, `review`, `post`, `benchmark`, `tools`, `call` |
| `tests/` | A miniature Zephyr-shaped repository fixture; intel, MCP (in-process and stdio), agent, publish tests |

## What is not done

- No live run yet. The agent is exercised against a scripted OpenRouter; the first real pull
  request and the first benchmark run will calibrate `max_steps`, cost, and the profile.
- Symbol lookup is regex over `git grep`. It is fast and good enough to start, and wrong on
  unusual formatting. A real index belongs in the shared service.
- The shared service (indexing, review history, project knowledge) does not exist. `review.json`
  and `transcript.jsonl` are written per run so there is history to load when it does.
- Using the GitHub MCP server alongside this one in Claude Code gives a maintainer GitHub state
  plus source intelligence; nothing here duplicates its tools.
- Incremental review of a force-pushed branch (what changed since the last review) is not wired
  in; `gh pr-diff` solves the hard part and is the natural next tool.

## License

Apache 2.0.
