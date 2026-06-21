"""Tests for the deterministic pasted-transcript attribution seam."""

from __future__ import annotations

from tl_agent.models import Engineer
from tl_agent.phases.standup_intake import attribute_pasted_standups

_TEAM = (
    Engineer(id="john", display_name="John Doe", aliases=("jdoe",)),
    Engineer(id="matt", display_name="Matt Stone"),
    Engineer(id="alicia", display_name="Alicia Park"),
)


def test_inline_headers_attribute_each_line() -> None:
    text = "John: shipped ENG-12\nMatt: on the auth refresh"
    assert attribute_pasted_standups(text, _TEAM) == {
        "john": "shipped ENG-12",
        "matt": "on the auth refresh",
    }


def test_own_line_header_then_body() -> None:
    text = "John:\nworked on ENG-12\nstill no blockers\nMatt: reviewing"
    out = attribute_pasted_standups(text, _TEAM)
    assert out["john"] == "worked on ENG-12\nstill no blockers"
    assert out["matt"] == "reviewing"


def test_unrecognised_label_stays_in_current_body() -> None:
    # `Status:` doesn't resolve to a team member, so it isn't a new section.
    text = "John: working\nStatus: green\nblockers: none"
    assert attribute_pasted_standups(text, _TEAM) == {
        "john": "working\nStatus: green\nblockers: none"
    }


def test_alias_resolves() -> None:
    assert attribute_pasted_standups("jdoe: hi", _TEAM) == {"john": "hi"}


def test_repeated_engineer_sections_join() -> None:
    text = "John: first thing\nMatt: x\nJohn: a later addition"
    out = attribute_pasted_standups(text, _TEAM)
    assert out["john"] == "first thing\n\na later addition"


def test_no_headers_returns_empty() -> None:
    assert attribute_pasted_standups("just some rambling, no names", _TEAM) == {}
