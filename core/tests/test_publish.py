"""The latest emitted board, copied to fixed names that git tracks."""

from __future__ import annotations

import json

from blpl.core import publish

BASE = "ear-tag_2026-10-16_124325Z"


def _emit(d, base=BASE, pcb="(kicad_pcb (version 20240108))\n"):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{base}.kicad_pcb").write_text(pcb)
    (d / f"{base}.kicad_sch").write_text('(kicad_sch (instances (project "ear-tag")))\n')
    (d / f"{base}.kicad_pro").write_text(json.dumps({
        "meta": {"filename": f"{base}.kicad_pro", "version": 1},
        "sheets": [["uuid-1", "Root"]],
        "schematic": {"top_level_sheets": [{"filename": f"{base}.kicad_sch", "name": base}]},
        # A string that merely contains the stem is not a file name.
        "text_variables": {"NOTE": f"from {base}"},
    }))


def test_the_project_name_is_the_stem_without_board_or_stamp():
    assert publish.project_name(BASE, None) == "ear-tag"
    assert publish.project_name("ph_core_2026-10-16_124325Z", "core") == "ph"
    # A project whose name ends like a board it is not about keeps its name.
    assert publish.project_name("my_core_2026-10-16_124325Z", None) == "my_core"


def test_publishes_one_fixed_name_per_kind(tmp_path):
    _emit(tmp_path / "pipe")
    out = publish.publish(tmp_path / "pipe", BASE, tmp_path / "kicad", "ear-tag")
    assert sorted(p.name for p in out.values()) == [
        "ear-tag.kicad_pcb", "ear-tag.kicad_pro", "ear-tag.kicad_sch", "libs"]
    # Board and schematic byte for byte.
    assert out["pcb"].read_bytes() == (tmp_path / "pipe" / f"{BASE}.kicad_pcb").read_bytes()
    assert out["sch"].read_bytes() == (tmp_path / "pipe" / f"{BASE}.kicad_sch").read_bytes()


def test_the_project_file_names_its_new_files_and_nothing_else_changes(tmp_path):
    _emit(tmp_path / "pipe")
    out = publish.publish(tmp_path / "pipe", BASE, tmp_path / "kicad", "ear-tag")
    pro = json.loads(out["pro"].read_text())
    assert pro["meta"]["filename"] == "ear-tag.kicad_pro"
    assert pro["schematic"]["top_level_sheets"] == [{"filename": "ear-tag.kicad_sch", "name": "ear-tag"}]
    assert pro["text_variables"]["NOTE"] == f"from {BASE}"
    assert BASE not in json.dumps({k: v for k, v in pro.items() if k != "text_variables"})


def test_the_next_run_replaces_the_last(tmp_path):
    _emit(tmp_path / "pipe")
    publish.publish(tmp_path / "pipe", BASE, tmp_path / "kicad", "ear-tag")
    later = "ear-tag_2026-10-17_000000Z"
    _emit(tmp_path / "pipe", later, pcb="(kicad_pcb (routed))\n")
    publish.publish(tmp_path / "pipe", later, tmp_path / "kicad", "ear-tag")
    assert sorted(p.name for p in (tmp_path / "kicad").iterdir()) == [
        "ear-tag.kicad_pcb", "ear-tag.kicad_pro", "ear-tag.kicad_sch",
        "fp-lib-table", "libs", "sym-lib-table"]
    assert (tmp_path / "kicad" / "ear-tag.kicad_pcb").read_text() == "(kicad_pcb (routed))\n"


def test_an_unchanged_board_is_not_rewritten(tmp_path):
    _emit(tmp_path / "pipe")
    out = publish.publish(tmp_path / "pipe", BASE, tmp_path / "kicad", "ear-tag")
    before = out["pcb"].stat().st_mtime_ns
    publish.publish(tmp_path / "pipe", BASE, tmp_path / "kicad", "ear-tag")
    assert out["pcb"].stat().st_mtime_ns == before


SCH_WITH_CACHE = """(kicad_sch
  (version 20260306)
  (lib_symbols
    (symbol "Device:R" (pin_names (offset 0)) (symbol "R_0_1" (rectangle (start -1 -2) (end 1 2))))
    (symbol "power:PWR_FLAG" (power) (symbol "PWR_FLAG_0_0" (pin power_out line (at 0 0 90))))
  )
)
"""


def _emit_with_libs(tmp_path):
    pipe = tmp_path / "pipe"
    _emit(pipe, pcb='(kicad_pcb (footprint "Resistor_SMD:R_0402" (layer "F.Cu")) '
                    '(footprint "Gone:Nowhere" (layer "F.Cu")))\n')
    (pipe / f"{BASE}.kicad_sch").write_text(SCH_WITH_CACHE)
    src = tmp_path / "stock" / "Resistor_SMD.pretty"
    src.mkdir(parents=True)
    (src / "R_0402.kicad_mod").write_text('(footprint "R_0402")\n')
    (src / "R_0603.kicad_mod").write_text('(footprint "R_0603")\n')
    (pipe / "fp-lib-table").write_text(
        f'(fp_lib_table (version 7) (lib (name "Resistor_SMD")(type "KiCad")(uri "{src}")(options "")(descr "")))\n')
    return pipe


def test_the_project_carries_the_symbols_it_uses_under_their_nicknames(tmp_path):
    from blpl.emitter import sexpr

    pipe = _emit_with_libs(tmp_path)
    publish.publish(pipe, BASE, tmp_path / "kicad", "ear-tag")
    libs = tmp_path / "kicad" / "libs"
    device = sexpr.parse((libs / "Device.kicad_sym").read_text())
    names = [sexpr.unquote(s[1]) for s in sexpr.find_all(device, "symbol")]
    # Named bare in its library, as the cache's "Device:R" resolves to.
    assert names == ["R"]
    assert (libs / "power.kicad_sym").is_file()
    table = (tmp_path / "kicad" / "sym-lib-table").read_text()
    assert '"${KIPRJMOD}/libs/Device.kicad_sym"' in table
    # No absolute path from the machine that built it.
    assert str(tmp_path) not in table


def test_the_project_carries_only_the_footprints_it_uses_and_says_which_it_could_not(tmp_path, capsys):
    pipe = _emit_with_libs(tmp_path)
    publish.publish(pipe, BASE, tmp_path / "kicad", "ear-tag")
    pretty = tmp_path / "kicad" / "libs" / "Resistor_SMD.pretty"
    assert sorted(p.name for p in pretty.iterdir()) == ["R_0402.kicad_mod"]
    assert '"${KIPRJMOD}/libs/Resistor_SMD.pretty"' in (tmp_path / "kicad" / "fp-lib-table").read_text()
    assert "Gone:Nowhere" in capsys.readouterr().out


def test_a_library_the_next_run_no_longer_uses_is_gone(tmp_path):
    pipe = _emit_with_libs(tmp_path)
    publish.publish(pipe, BASE, tmp_path / "kicad", "ear-tag")
    (pipe / f"{BASE}.kicad_pcb").write_text("(kicad_pcb)\n")
    publish.publish(pipe, BASE, tmp_path / "kicad", "ear-tag")
    assert not (tmp_path / "kicad" / "libs" / "Resistor_SMD.pretty").exists()
    assert (tmp_path / "kicad" / "libs" / "Device.kicad_sym").is_file()


def test_a_partial_emit_publishes_nothing(tmp_path, capsys):
    _emit(tmp_path / "pipe")
    (tmp_path / "pipe" / f"{BASE}.kicad_sch").unlink()
    assert publish.publish(tmp_path / "pipe", BASE, tmp_path / "kicad", "ear-tag") == {}
    assert not (tmp_path / "kicad").exists()
    assert "kicad_sch not emitted" in capsys.readouterr().out


def test_footprints_come_from_the_root_the_board_was_built_from(tmp_path):
    # stage6-plugin --footprints-root: the table's same-named footprint is not
    # the one on the board.
    pipe = _emit_with_libs(tmp_path)
    other = tmp_path / "override" / "Resistor_SMD.pretty"
    other.mkdir(parents=True)
    (other / "R_0402.kicad_mod").write_text('(footprint "R_0402" (override))\n')
    publish.publish(pipe, BASE, tmp_path / "kicad", "ear-tag", footprints_root=tmp_path / "override")
    got = (tmp_path / "kicad" / "libs" / "Resistor_SMD.pretty" / "R_0402.kicad_mod").read_text()
    assert "(override)" in got
