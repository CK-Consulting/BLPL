"""`blpl skills` — installing the Claude Code skill set into a project.

Unit tests drive skills_install against a fabricated kicad-happy tree so they
don't couple to the submodule's contents; one discovery test uses the real
submodule and skips when it isn't checked out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blpl.core import skills_install


@pytest.fixture
def fake_source(tmp_path: Path) -> Path:
    """A minimal kicad-happy-shaped tree: skills/<name>/SKILL.md + extras."""
    base = tmp_path / "kicad-happy"
    for name in ("kicad", "emc", "digikey"):
        d = base / "skills" / name
        (d / "scripts").mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nguidance\n")
        (d / "scripts" / "tool.py").write_text("print('hi')\n")
    # A directory without SKILL.md must not be offered as a skill.
    (base / "skills" / "not-a-skill").mkdir()
    return base


def test_available_lists_package_and_source_skills(fake_source) -> None:
    avail = skills_install.available(fake_source)
    assert "hardware-design" in avail  # ships inside the blpl package
    assert {"kicad", "emc", "digikey"} <= set(avail)
    assert "not-a-skill" not in avail


def test_install_copies_skill_with_provenance_marker(tmp_path, fake_source) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    result = skills_install.install(proj, ["kicad", "hardware-design"], source=fake_source)
    assert sorted(result.installed) == ["hardware-design", "kicad"]

    dest = proj / ".claude" / "skills" / "kicad"
    assert (dest / "SKILL.md").exists()
    assert (dest / "scripts" / "tool.py").exists()  # the whole tree travels
    assert (dest / ".blpl-installed").exists()      # provenance marker


def test_install_refuses_to_clobber_without_force(tmp_path, fake_source) -> None:
    """A project may carry a hand-edited copy; overwriting it silently would be
    quiet data loss. Untouched without --force, replaced with it."""
    proj = tmp_path / "proj"
    hand_edited = proj / ".claude" / "skills" / "kicad"
    hand_edited.mkdir(parents=True)
    (hand_edited / "SKILL.md").write_text("my local tweaks\n")

    result = skills_install.install(proj, ["kicad"], source=fake_source)
    assert result.skipped == ["kicad"] and not result.installed
    assert (hand_edited / "SKILL.md").read_text() == "my local tweaks\n"

    result = skills_install.install(proj, ["kicad"], source=fake_source, force=True)
    assert result.refreshed == ["kicad"]
    assert "guidance" in (hand_edited / "SKILL.md").read_text()


def test_unknown_skills_are_reported_not_ignored(tmp_path, fake_source) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    result = skills_install.install(proj, ["kicad", "nonesuch"], source=fake_source)
    assert result.unknown == ["nonesuch"]
    assert result.installed == ["kicad"]


def test_installed_reports_marker_state(tmp_path, fake_source) -> None:
    proj = tmp_path / "proj"
    skills_install.install(proj, ["kicad"], source=fake_source)
    # A skill someone put there by hand, no marker.
    hand = proj / ".claude" / "skills" / "own-skill"
    hand.mkdir(parents=True)
    (hand / "SKILL.md").write_text("mine\n")

    state = skills_install.installed(proj)
    assert state == {"kicad": True, "own-skill": False}


def test_default_set_is_review_oriented(fake_source) -> None:
    """Sourcing/fab skills activate on broad vocabulary; they stay opt-in."""
    assert "hardware-design" in skills_install.DEFAULT_SET
    assert "kicad" in skills_install.DEFAULT_SET
    assert "digikey" not in skills_install.DEFAULT_SET


def test_real_submodule_discovery() -> None:
    repo_kh = Path(__file__).resolve().parents[1] / "kicad-happy"
    if not (repo_kh / "skills" / "kicad" / "SKILL.md").exists():
        pytest.skip("kicad-happy submodule not checked out")
    avail = skills_install.available()
    assert "kicad" in avail and "emc" in avail
