"""Shared run-day resolution for the web UI.

The selected day is global and sticky: the sidebar's "Run day" control writes
it to the URL (`?date=`), and we persist it in the `tl_run_date` cookie so it
survives navigating between tabs (nav links also embed the date for immediate
correctness, but the cookie is the fallback for any link that doesn't).

Resolution order: explicit `?date=` query → `tl_run_date` cookie → today.
Routes call `resolve_date(request)` to read it and `set_date_cookie(response,
iso)` to persist an explicit pick.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import date

COOKIE_NAME = "tl_run_date"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _valid(value: str | None) -> str | None:
    if not value:
        return None
    with contextlib.suppress(ValueError):
        return date.fromisoformat(value).isoformat()
    return None


def resolve_date(request: object | None, date_param: str | None = None) -> str:
    """Resolve the selected run day: ?date → cookie → today.

    `request` is a Starlette/FastAPI Request (typed loosely to avoid importing
    it here); `date_param` is the route's `date` query value when already bound.
    """
    explicit = _valid(date_param)
    if explicit:
        return explicit
    if request is not None:
        cookies = getattr(request, "cookies", {}) or {}
        cookie = _valid(cookies.get(COOKIE_NAME))
        if cookie:
            return cookie
    return date.today().isoformat()


def cookie_date(request: object | None) -> str | None:
    """The persisted run day from the cookie, if valid — else None.

    Lets a route keep its own no-data / latest-snapshot fallback while still
    honouring the global sticky day when no `?date` is given.
    """
    if request is None:
        return None
    cookies = getattr(request, "cookies", {}) or {}
    return _valid(cookies.get(COOKIE_NAME))


def set_date_cookie(response: object, iso: str) -> None:
    """Persist the selected day on the response (best-effort; ignores non-cookie
    responses such as a bare string)."""
    setter = getattr(response, "set_cookie", None)
    if setter is None:
        return
    setter(key=COOKIE_NAME, value=iso, max_age=_COOKIE_MAX_AGE, samesite="lax")


def available_dates(conn: sqlite3.Connection) -> list[str]:
    """Distinct run dates that have a run on record — for the sidebar chips."""
    rows = conn.execute(
        "SELECT DISTINCT run_date FROM runs ORDER BY run_date DESC LIMIT 30"
    ).fetchall()
    return [r["run_date"] for r in rows]
