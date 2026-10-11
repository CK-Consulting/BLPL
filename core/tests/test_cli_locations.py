"""Where the CLI finds the two things that are no longer inside this package.

The KiCad plugin (`stage6-plugin`) and the app backend (`serve`) used to be
found as fixed paths inside one repository. They are separate trees now, so
each is looked up: an explicit variable first, then the umbrella layout. The
property worth pinning is that a variable naming the wrong place is reported,
not silently replaced by a guess that happens to exist.
"""

from __future__ import annotations

from pathlib import Path

from blpl.core import cli


def _fake_plugin(root: Path) -> Path:
    pkg = root / cli._PLUGIN_PACKAGE_NAME
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "build_pcb.py").write_text("")
    return root


def test_the_plugin_variable_wins(tmp_path, monkeypatch) -> None:
    root = _fake_plugin(tmp_path / "elsewhere")
    monkeypatch.setenv("BLPL_KICAD_PLUGIN_DIR", str(root))
    assert cli._resolve_plugin_dir() == root


def test_a_plugin_variable_naming_the_wrong_place_is_not_overridden(tmp_path, monkeypatch) -> None:
    """Falling through to the umbrella copy would run a different plugin from
    the one asked for, and say nothing about it."""
    monkeypatch.setenv("BLPL_KICAD_PLUGIN_DIR", str(tmp_path / "nothing-here"))
    assert cli._resolve_plugin_dir() is None


def test_without_a_variable_the_umbrella_plugin_is_found(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BLPL_KICAD_PLUGIN_DIR", raising=False)
    monkeypatch.setattr(cli, "_REPO_ROOT", tmp_path / "core")
    monkeypatch.setattr(cli, "_PLUGIN_PACKAGE_NAME", "blpl_kicad_plugin_test_only")
    root = _fake_plugin(tmp_path / "plugin")
    assert cli._resolve_plugin_dir() == root


def test_no_plugin_anywhere_is_none_not_a_guess(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BLPL_KICAD_PLUGIN_DIR", raising=False)
    monkeypatch.setattr(cli, "_REPO_ROOT", tmp_path / "core")
    monkeypatch.setattr(cli, "_PLUGIN_PACKAGE_NAME", "blpl_kicad_plugin_test_only")
    assert cli._resolve_plugin_dir() is None


def test_stage6_plugin_refuses_when_the_plugin_is_missing(tmp_path, monkeypatch, capsys) -> None:
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / ".pipeline" / "hdm.yaml").write_text("components: {}\n")
    monkeypatch.setenv("BLPL_KICAD_PLUGIN_DIR", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(cli, "_resolve_kicad_python", lambda _v: Path("/bin/true"))
    rc = cli.main(["stage6-plugin", "--project-dir", str(proj)])
    assert rc == 2
    assert "BLPL_KICAD_PLUGIN_DIR" in capsys.readouterr().err


def test_the_backend_variable_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLPL_APP_BACKEND", str(tmp_path / "backend"))
    assert cli._resolve_backend_dir() == (tmp_path / "backend").resolve()


def test_without_a_variable_the_backend_is_beside_this_checkout(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BLPL_APP_BACKEND", raising=False)
    monkeypatch.setattr(cli, "_REPO_ROOT", tmp_path / "core")
    assert cli._resolve_backend_dir() == tmp_path / "app" / "backend"
