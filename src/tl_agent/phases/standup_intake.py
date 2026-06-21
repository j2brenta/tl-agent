"""Standup intake — turn a pasted multi-engineer transcript into per-engineer
raw text.

This is the deterministic seam every "paste one blob" standup source funnels
through: the manual form today, and the messenger-paste agent later (which will
swap these header heuristics for LLM name-attribution + date resolution while
keeping this signature). Output feeds the persist-and-segment funnel
(`web.routes.workflow._persist_manual_standups`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from tl_agent.models import Engineer

# A header at the start of a line: `John:` / `Matt -` / `Alicia —`, optionally
# followed by inline content on the same line. Gated by `Engineer.matches()`,
# so non-name labels (`Status:`, `Blockers:`) fall through into the current
# section's body instead of starting a new one.
_HEADER_RE = re.compile(r"^[ \t]*([A-Za-z][\w\-]*)[ \t]*[:\-—][ \t]*(.*)$", re.MULTILINE)


def attribute_pasted_standups(text: str, engineers: Iterable[Engineer]) -> dict[str, str]:
    """Map a pasted transcript to ``{engineer_id: raw}``.

    Splits on ``Name:`` headers (own-line or inline) that resolve to a team
    member via :meth:`Engineer.matches`; each engineer's body runs to the next
    recognised header. Repeated headers for one engineer are joined. Returns an
    empty dict when nothing attributes — the caller surfaces that as guidance.
    """
    eng_list = list(engineers)
    # (header_start, inline_content_end, engineer_id, inline_content)
    headers: list[tuple[int, int, str, str]] = []
    for m in _HEADER_RE.finditer(text):
        engineer = next((e for e in eng_list if e.matches(m.group(1))), None)
        if engineer is None:
            continue
        headers.append((m.start(), m.end(2), engineer.id, m.group(2)))
    if not headers:
        return {}

    by_engineer: dict[str, list[str]] = {}
    for i, (_, inline_end, eid, inline) in enumerate(headers):
        next_start = headers[i + 1][0] if i + 1 < len(headers) else len(text)
        # `text[inline_end:...]` already begins at the newline after the inline
        # content (empty for an own-line header), so no separator is needed.
        body = (inline + text[inline_end:next_start]).strip()
        if body:
            by_engineer.setdefault(eid, []).append(body)
    return {eid: "\n\n".join(bodies) for eid, bodies in by_engineer.items()}
