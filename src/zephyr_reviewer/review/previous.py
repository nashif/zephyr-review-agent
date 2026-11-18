"""The record of the run that reviewed this pull request before, and what a re-review takes from it.

A run saves ``review.json`` (what it reviewed and found) and ``review_map.json`` (every candidate and its
fate). The next run reads them back, from the workflow run noted in the summary comment or from a
directory, and uses three things: the per-hunk digests, to tell what changed since (``patch.delta_since``);
the findings that live only in the summary comment, to check them again; and the candidates that were
investigated and refuted, so the same investigation is not paid for twice."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .discussion import sanitize
from .external import EARLIER_SUMMARY, MAX_CLAIMS, Claim
from .followup import same_title
from .github_api import GitHub, GitHubError
from .patch import Delta
from .render import inline_marker, normalize_title, posted_title

MAX_REFUTED = 60
MAX_REASON_CHARS = 400
MAX_ARTIFACT_BYTES = 50_000_000
MAX_MEMBER_BYTES = 20_000_000


@dataclass
class Refutation:
    """A candidate an earlier run investigated and refuted."""

    file: str
    line: int
    title: str
    reason: str
    head: str = ""  # the head commit at which it was refuted
    settled: bool = False  # the file's patch is the one it was refuted on: it stands without a new check

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("settled")
        return d


@dataclass
class Previous:
    head_sha: str
    change: dict[str, Any]
    findings: list[dict[str, Any]] = field(default_factory=list)
    refuted: list[Refutation] = field(default_factory=list)
    assessments: dict[str, str] = field(default_factory=dict)
    origin: str = ""


def parse_record(record: dict[str, Any], review_map: dict[str, Any] | None, origin: str = "") -> Previous | None:
    """A saved run as ``Previous``; None when it did not complete or predates the change digests."""
    review = record.get("review")
    change = record.get("change")
    if not isinstance(review, dict) or not isinstance(change, dict) or not isinstance(change.get("files"), dict):
        return None
    head = str(record.get("head_sha") or "")
    refuted: list[Refutation] = []
    seen: set[tuple[str, str]] = set()

    def add(file: Any, line: Any, title: Any, reason: Any, at: Any) -> None:
        key = (str(file or ""), normalize_title(str(title or "")))
        if not key[1] or key in seen or len(refuted) >= MAX_REFUTED:
            return
        seen.add(key)
        try:
            n = int(line or 0)
        except (TypeError, ValueError):
            n = 0
        refuted.append(Refutation(key[0], n, str(title).strip(), str(reason or "").strip(), str(at or head)))

    m = review_map if isinstance(review_map, dict) else {}
    for c in m.get("concerns") or []:
        if isinstance(c, dict) and c.get("fate") == "refuted":
            add(c.get("file"), c.get("line"), c.get("title"), c.get("reason"), head)
    for c in m.get("refuted_earlier") or []:
        if isinstance(c, dict):
            add(c.get("file"), c.get("line"), c.get("title"), c.get("reason"), c.get("head"))
    return Previous(
        head_sha=head,
        change=change,
        findings=[f for f in review.get("findings") or [] if isinstance(f, dict)],
        refuted=refuted,
        assessments={
            k: str(review.get(k) or "").strip()
            for k in ("test_assessment", "commit_message_assessment")
            if str(review.get(k) or "").strip()
        },
        origin=origin,
    )


def load_dir(path: str) -> Previous | None:
    """A record saved in a directory (an earlier ``--output-dir``)."""
    d = Path(path)
    try:
        record = json.loads((d / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        review_map = json.loads((d / "review_map.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        review_map = None
    return parse_record(record, review_map, origin=str(d)) if isinstance(record, dict) else None


def _from_zip(blob: bytes) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    record = review_map = None
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            name = info.filename.rsplit("/", 1)[-1]
            if name not in ("review.json", "review_map.json") or info.file_size > MAX_MEMBER_BYTES:
                continue
            try:
                data = json.loads(z.read(info).decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if name == "review.json":
                record = data
            else:
                review_map = data
    return record, review_map


def fetch_run(gh: GitHub, run_id: str, pr_number: int, log=print) -> Previous | None:
    """The record among the artifacts of workflow run ``run_id``. Needs ``actions: read``; an expired
    artifact, a missing permission or a record of another pull request all give None."""
    try:
        artifacts = gh.run_artifacts(run_id)
    except GitHubError as exc:
        log(f"the earlier run's artifacts cannot be listed ({str(exc)[:160]})")
        return None
    usable = [a for a in artifacts if not a.get("expired") and int(a.get("size_in_bytes") or 0) <= MAX_ARTIFACT_BYTES]
    usable.sort(key=lambda a: str(pr_number) not in str(a.get("name") or ""))
    for a in usable[:5]:
        try:
            record, review_map = _from_zip(gh.artifact_zip(int(a["id"])))
        except (GitHubError, zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
            log(f"artifact {a.get('name')!r} of the earlier run cannot be read ({str(exc)[:160]})")
            continue
        if record is None or int(record.get("pr") or 0) != pr_number:
            continue
        # A run that was attempted twice has two artifacts of one name: the failed attempt's is not usable.
        prev = parse_record(record, review_map, origin=f"artifact {a.get('name')!r} of run {run_id}")
        if prev is not None:
            return prev
    return None


# ------------------------------------------------------------------ refuted earlier
def settle(refuted: list[Refutation], delta: Delta) -> list[Refutation]:
    """Mark what still stands: a refutation in a file whose patch did not change is settled; one in a
    file the change no longer touches is gone."""
    unchanged, dropped = set(delta.unchanged), set(delta.dropped)
    out = []
    for r in refuted:
        if r.file and r.file in dropped:
            continue
        r.settled = bool(r.file) and r.file in unchanged
        out.append(r)
    return out


def refuted_digest(refuted: list[Refutation]) -> str:
    """The block the models see. It is the tool's own earlier output, derived from untrusted input."""
    lines = []
    for r in refuted:
        loc = f"{r.file}:{r.line}" if r.file and r.line else (r.file or "general")
        state = "file unchanged since" if r.settled else "file changed since"
        reason = " ".join(sanitize(r.reason).split())[:MAX_REASON_CHARS]
        lines.append(f'- {loc} "{" ".join(sanitize(r.title).split())}" ({state}): {reason or "(no reason kept)"}')
    return "\n".join(lines)


def matching_refutation(file: str, title: str, refuted: list[Refutation]) -> Refutation | None:
    """The settled refutation a candidate restates: same file, same or contained title."""
    for r in refuted:
        if r.settled and r.file == file and same_title(title, r.title):
            return r
    return None


# ------------------------------------------------------------------ findings posted only in the summary
def _key(file: str, title: str) -> str:
    return hashlib.sha1(f"{file}:{normalize_title(title)}".encode()).hexdigest()[:12]  # as render.finding_key


def summary_only(prev: Previous, gh: GitHub, number: int, tag: str) -> list[dict[str, Any]]:
    """The earlier findings that have no inline comment: nothing else carries them into this run. One
    with a thread is handled through that thread, whatever its state."""
    prefix = inline_marker(tag, "")[:-4]  # "<!-- tag:finding:"
    ours = [c for c in gh.review_comments(number) if prefix in (c.get("body") or "")]
    bodies = "\n".join(c.get("body") or "" for c in ours)
    titles = {(c.get("path") or "", posted_title(c.get("body") or "")) for c in ours}
    out = []
    for f in prev.findings:
        file, title = str(f.get("file") or ""), str(f.get("title") or "")
        if inline_marker(tag, _key(file, title)) in bodies or (file, normalize_title(title)) in titles:
            continue
        out.append(f)
    return out


def carried_claims(findings: list[dict[str, Any]], start: int, old_head: str) -> list[Claim]:
    """Earlier summary-only findings as claims ``X{start}``...: confirmed ones are findings again,
    refuted ones leave the summary."""
    source = f"{EARLIER_SUMMARY}, at {old_head[:12]}" if old_head else EARLIER_SUMMARY
    claims = []
    for i, f in enumerate(findings[:MAX_CLAIMS]):
        title = str(f.get("title") or "").strip()
        parts = [str(f.get(k) or "").strip() for k in ("problem", "trigger", "impact")]
        text = f"{title}: " + " ".join(p for p in parts if p)
        extra = {k: f[k] for k in ("severity", "category", "trigger", "impact", "recommendation") if f.get(k)}
        extra["title"] = title
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        claims.append(Claim(f"X{start + i}", text.strip(), str(f.get("file") or ""), line, source, extra))
    return claims
