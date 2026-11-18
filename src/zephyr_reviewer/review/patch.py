"""What a pull request changes, in a form that survives a rebase: a digest of the whole change, so a push
that changes nothing is not reviewed twice, and digests per hunk, so a later run can tell what is new."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_GIT_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")


def _norm(line: str) -> str | None:
    """A diff line without what moves when the base moves: blob ids are dropped, hunk positions blanked."""
    if line.startswith("index "):
        return None
    if line.startswith("@@"):
        return "@@"
    return line


def _sha(lines: Iterable[str]) -> str:
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8", "replace") + b"\n")
    return h.hexdigest()[:16]


def messages_digest(commits: Iterable[Any]) -> str:
    """Commit messages only: ids, authors and dates change on a rebase and are left out."""
    h = hashlib.sha256()
    for c in commits:
        h.update(b"\0commit\0")
        h.update(f"{getattr(c, 'subject', '')}\n{getattr(c, 'body', '')}".strip().encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def patch_digest(diff_text: str, commits: Iterable[Any] = ()) -> str:
    """Identify the change by its content: the diff without what a rebase alters, plus the commit messages.

    Blob ids and hunk positions move when the base moves, so they are left out; the changed lines and their
    context are kept, which makes a rebase that altered the surrounding code count as a different change.
    Commit messages are included because the review reads them: a reworded message is reviewed again.
    Commit ids, authors and dates are not."""
    h = hashlib.sha256()
    for line in diff_text.splitlines():
        n = _norm(line)
        if n is not None:
            h.update(n.encode("utf-8", "replace") + b"\n")
    for c in commits:
        h.update(b"\0commit\0")
        h.update(f"{getattr(c, 'subject', '')}\n{getattr(c, 'body', '')}".strip().encode("utf-8", "replace"))
    return h.hexdigest()[:32]


@dataclass
class FilePatch:
    path: str
    header: list[str]  # "diff --git" up to the first hunk
    hunks: list[list[str]]  # each starts with its "@@" line

    @property
    def header_digest(self) -> str:
        return "h:" + _sha(n for n in map(_norm, self.header) if n is not None)

    def hunk_digest(self, i: int) -> str:
        return _sha(n for n in map(_norm, self.hunks[i]) if n is not None)

    @property
    def digests(self) -> list[str]:
        return [self.header_digest, *(self.hunk_digest(i) for i in range(len(self.hunks)))]


def split_patch(diff_text: str) -> list[FilePatch]:
    """The diff as files and hunks, text kept verbatim."""
    out: list[FilePatch] = []
    cur: FilePatch | None = None
    for line in diff_text.splitlines():
        m = _GIT_HEADER.match(line)
        if m:
            cur = FilePatch(m.group(2), [line], [])
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("@@"):
            cur.hunks.append([line])
        elif cur.hunks:
            cur.hunks[-1].append(line)
        else:
            cur.header.append(line)
    return out


def describe_change(diff_text: str, commits: Iterable[Any] = ()) -> dict[str, Any]:
    """What a run's record keeps about the change it reviewed, for the next run to compare against."""
    commits = list(commits)
    return {
        "digest": patch_digest(diff_text, commits),
        "messages": messages_digest(commits),
        "files": {f.path: f.digests for f in split_patch(diff_text)},
    }


def _changed_lines(lines: Iterable[str]) -> int:
    return sum(1 for ln in lines if ln[:1] in "+-" and not ln.startswith(("+++", "---")))


@dataclass
class Delta:
    """The part of a change that an earlier review has not seen."""

    files: list[str] = field(default_factory=list)  # files with something new, in diff order
    unchanged: list[str] = field(default_factory=list)  # files whose patch is the one reviewed earlier
    dropped: list[str] = field(default_factory=list)  # files the change no longer touches
    diff_text: str = ""  # the new hunks only, under their file headers
    lines: int = 0  # added and removed lines in the new hunks
    total_lines: int = 0  # added and removed lines in the whole change
    messages_changed: bool = False

    @property
    def share(self) -> float:
        return self.lines / self.total_lines if self.total_lines else 0.0

    @property
    def empty(self) -> bool:
        return not self.files and not self.messages_changed


def delta_since(earlier: dict[str, Any], diff_text: str, commits: Iterable[Any] = ()) -> Delta:
    """Compare the change now with ``earlier`` (a record's ``describe_change``): which hunks are new.

    A hunk counts as reviewed when its text, context included, was part of the same file's patch then;
    where it sits in the file does not matter. A file whose header changed (renamed, mode) is new."""
    was: dict[str, list[str]] = {str(k): list(v) for k, v in (earlier.get("files") or {}).items()}
    d = Delta(messages_changed=str(earlier.get("messages") or "") != messages_digest(commits))
    out: list[str] = []
    seen: set[str] = set()
    for f in split_patch(diff_text):
        seen.add(f.path)
        d.total_lines += sum(_changed_lines(h) for h in f.hunks)
        old = set(was.get(f.path, []))
        new_header = f.header_digest not in old
        fresh = [h for i, h in enumerate(f.hunks) if f.hunk_digest(i) not in old]
        # A hunk the author removed leaves nothing to read, but the file is not what was reviewed.
        removed = bool(old - set(f.digests))
        if not new_header and not fresh and not removed:
            d.unchanged.append(f.path)
            continue
        d.files.append(f.path)
        if new_header or fresh:
            out += f.header
            for h in fresh:
                out += h
                d.lines += _changed_lines(h)
    d.dropped = [p for p in was if p not in seen]
    d.diff_text = "\n".join(out)
    return d
