"""Local-override overlay for LAYER 1 config.

A file dropped in `local_config_dir` shadows the committed default of the same
name; everything else keeps tracking `config_dir`. This is what keeps a
`git pull` from clobbering per-deployment config (roster, allowlists, board id).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_agent.settings import get_settings
from tl_agent.storage.markdown_loader import load_team

_DEFAULT_TEAM = """\
## Sprint scope

- **board_id:** ENG
- **sprint_name_pattern:** Eng Sprint .*
"""

_OVERRIDE_TEAM = """\
## Sprint scope

- **board_id:** PROD
- **sprint_name_pattern:** Prod Sprint .*
"""


@pytest.fixture
def config_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    committed = tmp_path / "config"
    local = tmp_path / "local"
    committed.mkdir()
    local.mkdir()
    (committed / "team.md").write_text(_DEFAULT_TEAM, encoding="utf-8")
    monkeypatch.setenv("TLA_CONFIG_DIR", str(committed))
    monkeypatch.setenv("TLA_LOCAL_CONFIG_DIR", str(local))
    return committed, local


def test_falls_back_to_committed_default(config_dirs: tuple[Path, Path]) -> None:
    committed, _ = config_dirs
    assert get_settings().resolve_config("team.md") == committed / "team.md"
    assert load_team().board_id == "ENG"


def test_local_file_shadows_default(config_dirs: tuple[Path, Path]) -> None:
    _, local = config_dirs
    (local / "team.md").write_text(_OVERRIDE_TEAM, encoding="utf-8")
    assert get_settings().resolve_config("team.md") == local / "team.md"
    assert load_team().board_id == "PROD"


def test_overlay_is_per_file(config_dirs: tuple[Path, Path]) -> None:
    committed, local = config_dirs
    # only ownership is overridden locally; team.md still resolves to committed
    (local / "ownership.md").write_text("owned\n", encoding="utf-8")
    assert get_settings().resolve_config("ownership.md") == local / "ownership.md"
    assert get_settings().resolve_config("team.md") == committed / "team.md"


def test_explicit_config_dir_bypasses_overlay(config_dirs: tuple[Path, Path]) -> None:
    _, local = config_dirs
    (local / "team.md").write_text(_OVERRIDE_TEAM, encoding="utf-8")
    # passing config_dir explicitly (the test/web path) ignores the local dir
    assert load_team(config_dir=config_dirs[0]).board_id == "ENG"
