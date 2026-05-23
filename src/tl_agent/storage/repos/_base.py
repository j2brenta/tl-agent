"""Tiny shared helpers for the repo layer.

We don't have a base class — each repo is a module of plain functions taking
the connection. This keeps them composable, easy to test, and avoids hiding
SQL behind methods.
"""

from __future__ import annotations

import json
from typing import Any


def to_json(value: Any) -> str:
    """Compact JSON encode with sorted keys (so payload hashes are stable)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def from_json(text: str) -> Any:
    """JSON decode."""
    return json.loads(text)
