# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Text as it may travel: a model's output can hold what nothing downstream can encode."""

from __future__ import annotations

from typing import Any


def without_surrogates(value: Any) -> Any:
    """``value`` with every lone surrogate in its strings replaced. A JSON escape such as ``\\ud83d`` in a
    model's reply becomes a lone surrogate in Python, which is not UTF-8: sent as a tool argument it kills
    the MCP transport (pydantic cannot serialize it), and written to a file or posted it fails the same
    way. Dicts and lists are walked; anything else is returned as is."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
        return value
    if isinstance(value, dict):
        return {k: without_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [without_surrogates(v) for v in value]
    return value
