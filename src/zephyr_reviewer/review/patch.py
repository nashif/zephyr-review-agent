"""A digest of what a pull request changes, so a push that changes nothing is not reviewed twice."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any


def patch_digest(diff_text: str, commits: Iterable[Any] = ()) -> str:
    """Identify the change by its content: the diff without what a rebase alters, plus the commit messages.

    Blob ids and hunk positions move when the base moves, so they are left out; the changed lines and their
    context are kept, which makes a rebase that altered the surrounding code count as a different change.
    Commit messages are included because the review reads them: a reworded message is reviewed again.
    Commit ids, authors and dates are not."""
    h = hashlib.sha256()
    for line in diff_text.splitlines():
        if line.startswith("index "):
            continue
        if line.startswith("@@"):
            line = "@@"
        h.update(line.encode("utf-8", "replace") + b"\n")
    for c in commits:
        h.update(b"\0commit\0")
        h.update(f"{getattr(c, 'subject', '')}\n{getattr(c, 'body', '')}".strip().encode("utf-8", "replace"))
    return h.hexdigest()[:32]
