"""Uploaded KiCad library files land where the resolver looks, or are held.

A symbol or footprint from a vendor site used to be fileable only as a
reference, where nothing in the pipeline would ever find it. These tests pin
down the three things that make placing them safe: the file is parsed before it
is trusted to be what it says, nothing already in a library is replaced, and a
vendor zip cannot write outside the places it is routed to. And the point of it
all — that Stage 5's resolver and Stage 6's loaders find what was uploaded.
"""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from blpl.core import av, library_upload, quarantine
from blpl.core import symbol_resolution as sr
from blpl.emitter import loaders, sexpr

BENIGN_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _symbol(name: str, *, extends: str | None = None, pin: str = "1") -> str:
    body = f'(extends "{extends}")' if extends else (
        f'(symbol "{name}_1_1" (pin passive line (at 0 0 0) (length 2.54) '
        f'(name "A" (effects (font (size 1.27 1.27)))) (number "{pin}" (effects (font (size 1.27 1.27))))))'
    )
    return f'  (symbol "{name}" (in_bom yes) (on_board yes)\n    (property "Reference" "U" (at 0 0 0))\n    {body}\n  )\n'


def _symlib(*symbols: str, version: str = "20231120") -> bytes:
    return (f"(kicad_symbol_lib (version {version}) (generator kicad_symbol_editor)\n" + "".join(symbols) + ")\n").encode()


def _footprint(name: str, head: str = "footprint") -> bytes:
    return (
        f'({head} "{name}" (layer "F.Cu")\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        '  (model "PCM12SMTBR.stp" (offset (xyz 0 0 0)))\n'
        ")\n"
    ).encode()


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


@pytest.fixture(autouse=True)
def _no_scanner(monkeypatch):
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)
    monkeypatch.delenv("BLPL_QUARANTINE_REQUIRE_SCAN", raising=False)


def _up(project: Path, data: bytes, kind: str, name: str, library: str = "myproj"):
    return quarantine.accept_upload(
        data, project, original_name=name, kind=kind, uploaded_by="a@example.com", library=library,
    )


def _stock(tmp_path: Path) -> Path:
    root = tmp_path / "stock"
    root.mkdir(exist_ok=True)
    return root


# --------------------------------------------------------------------------- symbols


def test_a_symbol_into_a_new_library_lands_as_uploaded_and_resolves(tmp_path):
    data = _symlib(_symbol("PCM12SMTBR"))
    rec = _up(tmp_path, data, "symbol", "PCM12SMTBR.kicad_sym")
    assert rec.state == quarantine.RELEASED, rec.reasons
    lib = tmp_path / "libraries" / "symbols" / "myproj.kicad_sym"
    # A whole file needs no transformation, so it is written byte for byte.
    assert lib.read_bytes() == data
    assert rec.placed == ["libraries/symbols/myproj.kicad_sym"]
    assert f"{rec.released_to}/{rec.released_as}" == "libraries/symbols/myproj.kicad_sym"
    assert rec.library == "myproj"
    # What all of this is for: Stage 5 resolves it as hand-authored, ahead of
    # stock, and Stage 6 loads it with its pins.
    res = sr.resolve("myproj:PCM12SMTBR", project_dir=tmp_path, stock_root=_stock(tmp_path))
    assert res.source == sr.CUSTOM
    roots = [r for r, _ in sr.search_path(tmp_path, tmp_path / "stock")]
    assert loaders.load_symbol_pins("myproj:PCM12SMTBR", roots)


def test_a_symbol_is_merged_into_an_existing_flat_library_through_the_parser(tmp_path):
    lib = tmp_path / "libraries" / "symbols" / "myproj.kicad_sym"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(_symlib(_symbol("MINE"), version="20211014"))

    rec = _up(tmp_path, _symlib(_symbol("VENDOR"), version="20231120"), "symbol", "v.kicad_sym")
    assert rec.state == quarantine.RELEASED, rec.reasons

    parsed = sexpr.parse(lib.read_text(encoding="utf-8"))
    names = [sexpr.unquote(s[1]) for s in sexpr.find_all(parsed, "symbol")]
    assert names == ["MINE", "VENDOR"]
    # The newer format wins, so the newer symbol is not declared as older syntax.
    assert sexpr.find(parsed, "version")[1] == "20231120"
    for name in names:
        res = sr.resolve(f"myproj:{name}", project_dir=tmp_path, stock_root=_stock(tmp_path))
        assert res.source == sr.CUSTOM


def test_merging_into_a_canonical_library_diffs_as_a_pure_addition(tmp_path):
    """Text is the source of truth because it diffs; a merge must not churn the file."""
    lib = tmp_path / "libraries" / "symbols" / "myproj.kicad_sym"
    lib.parent.mkdir(parents=True)
    lib.write_text(sexpr.dump_top(sexpr.parse(_symlib(_symbol("MINE")).decode())), encoding="utf-8")
    old = lib.read_text(encoding="utf-8")

    rec = _up(tmp_path, _symlib(_symbol("VENDOR")), "symbol", "v.kicad_sym")
    new = lib.read_text(encoding="utf-8")
    assert new.startswith(old[: -len(")\n")])
    assert "rewritten" not in rec.inspection["placement"]

    # A hand-indented library is normalised by the dump, and the record says so.
    lib.write_bytes(_symlib(_symbol("HAND")))
    rec = _up(tmp_path, _symlib(_symbol("ANOTHER")), "symbol", "a.kicad_sym")
    assert "rewritten in KiCad's canonical layout" in rec.inspection["placement"]


def test_a_symbol_goes_one_file_per_symbol_into_an_existing_symdir(tmp_path):
    symdir = tmp_path / "libraries" / "symbols" / "myproj.kicad_symdir"
    symdir.mkdir(parents=True)
    (symdir / "MINE.kicad_sym").write_bytes(_symlib(_symbol("MINE")))

    rec = _up(tmp_path, _symlib(_symbol("A"), _symbol("B")), "symbol", "two.kicad_sym")
    assert rec.state == quarantine.RELEASED, rec.reasons
    assert rec.placed == [
        "libraries/symbols/myproj.kicad_symdir/A.kicad_sym",
        "libraries/symbols/myproj.kicad_symdir/B.kicad_sym",
    ]
    for name in ("A", "B"):
        piece = sexpr.parse((symdir / f"{name}.kicad_sym").read_text(encoding="utf-8"))
        assert [sexpr.unquote(s[1]) for s in sexpr.find_all(piece, "symbol")] == [name]
        roots = [r for r, _ in sr.search_path(tmp_path, tmp_path / "stock")]
        assert loaders.load_symbol_pins(f"myproj:{name}", roots)
    assert not (tmp_path / "libraries" / "symbols" / "myproj.kicad_sym").exists()


def test_a_symbol_whose_name_is_taken_is_held_and_the_library_is_untouched(tmp_path):
    lib = tmp_path / "libraries" / "symbols" / "myproj.kicad_sym"
    lib.parent.mkdir(parents=True)
    original = _symlib(_symbol("R", pin="1"))
    lib.write_bytes(original)

    rec = _up(tmp_path, _symlib(_symbol("R", pin="2"), _symbol("NEW")), "symbol", "r.kicad_sym")
    assert rec.state == quarantine.HELD
    assert "already has a different R" in rec.reasons[0]
    assert "libraries/symbols/myproj.kicad_sym" in rec.reasons[0]
    # All or nothing: NEW was not slipped in beside the refusal.
    assert lib.read_bytes() == original
    assert (tmp_path / "retrieved" / rec.filename).is_file()


def test_the_same_symbol_uploaded_again_is_already_there_not_a_collision(tmp_path):
    data = _symlib(_symbol("X"))
    assert _up(tmp_path, data, "symbol", "x.kicad_sym").state == quarantine.RELEASED
    lib = tmp_path / "libraries" / "symbols" / "myproj.kicad_sym"
    before = lib.read_bytes()
    again = _up(tmp_path, data, "symbol", "x.kicad_sym")
    assert again.state == quarantine.RELEASED, again.reasons
    assert again.inspection.get("placement") == "already in the library, unchanged"
    assert lib.read_bytes() == before


def test_a_derived_symbol_without_its_parent_is_held(tmp_path):
    rec = _up(tmp_path, _symlib(_symbol("CHILD", extends="MISSING")), "symbol", "c.kicad_sym")
    assert rec.state == quarantine.HELD
    assert "extends MISSING" in rec.reasons[0]


def test_a_derived_symbol_whose_parent_is_in_the_library_is_accepted(tmp_path):
    _up(tmp_path, _symlib(_symbol("PARENT")), "symbol", "p.kicad_sym")
    rec = _up(tmp_path, _symlib(_symbol("CHILD", extends="PARENT")), "symbol", "c.kicad_sym")
    assert rec.state == quarantine.RELEASED, rec.reasons


@pytest.mark.parametrize(
    "data, why",
    [
        (b'(kicad_symbol_lib (version 1) (symbol "X"', "does not parse"),
        (_footprint("FP"), "top-level node is (footprint"),
        (_symlib(_symbol("X")) + b"(symbol \"Y\")", "expected one top-level list"),
        (b"\xff\xfe not text", "not UTF-8"),
        (b"(kicad_symbol_lib (version 20231120))", "holds no symbols"),
        (_symlib(_symbol("../evil")), "cannot be used as a file name"),
    ],
)
def test_a_file_that_is_not_a_symbol_library_is_held(tmp_path, data, why):
    rec = _up(tmp_path, data, "symbol", "x.kicad_sym")
    assert rec.state == quarantine.HELD
    assert why in rec.reasons[0]
    assert not (tmp_path / "libraries").exists()


def test_a_library_name_that_could_not_be_a_kicad_nickname_is_refused(tmp_path):
    for bad in ("", "a:b", "../x", "a/b", ".hidden", "x" * 65):
        with pytest.raises(ValueError):
            _up(tmp_path, _symlib(_symbol("X")), "symbol", "x.kicad_sym", library=bad)


def test_an_infected_symbol_is_held_before_it_is_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        quarantine.av, "scan_bytes", lambda data: av.ScanResult(state="infected", signature="Test-Sig")
    )
    rec = _up(tmp_path, _symlib(_symbol("X")), "symbol", "x.kicad_sym")
    assert rec.state == quarantine.HELD
    assert any("Test-Sig" in r for r in rec.reasons)
    assert not (tmp_path / "libraries").exists()


# ------------------------------------------------------------------------ footprints


def test_a_footprint_lands_under_its_own_name_and_resolves(tmp_path):
    data = _footprint("PCM12SMTBR")
    rec = _up(tmp_path, data, "footprint", "whatever-the-site-called-it.kicad_mod")
    assert rec.state == quarantine.RELEASED, rec.reasons
    target = tmp_path / "libraries" / "footprints" / "myproj.pretty" / "PCM12SMTBR.kicad_mod"
    assert target.read_bytes() == data
    assert rec.released_to == "libraries/footprints"
    assert rec.released_as == "myproj.pretty/PCM12SMTBR.kicad_mod"
    res = sr.resolve_footprint("myproj:PCM12SMTBR", project_dir=tmp_path, stock_root=_stock(tmp_path))
    assert res.source == sr.CUSTOM
    roots = [r for r, _ in sr.footprint_search_path(tmp_path, tmp_path / "stock")]
    assert loaders.extract_pads(loaders.load_footprint("myproj:PCM12SMTBR", roots))


def test_a_legacy_module_footprint_is_accepted_and_a_lib_prefix_dropped(tmp_path):
    rec = _up(tmp_path, _footprint("Vendor:SW_1", head="module"), "footprint", "sw.kicad_mod")
    assert rec.state == quarantine.RELEASED, rec.reasons
    assert (tmp_path / "libraries" / "footprints" / "myproj.pretty" / "SW_1.kicad_mod").is_file()


def test_a_different_footprint_with_a_taken_name_is_held(tmp_path):
    assert _up(tmp_path, _footprint("FP"), "footprint", "a.kicad_mod").state == quarantine.RELEASED
    other = _footprint("FP").replace(b"(size 1 1)", b"(size 2 2)")
    rec = _up(tmp_path, other, "footprint", "b.kicad_mod")
    assert rec.state == quarantine.HELD
    assert "already has a different footprint FP" in rec.reasons[0]
    target = tmp_path / "libraries" / "footprints" / "myproj.pretty" / "FP.kicad_mod"
    assert target.read_bytes() == _footprint("FP")
    # The identical one again is not a collision.
    assert _up(tmp_path, _footprint("FP"), "footprint", "a.kicad_mod").state == quarantine.RELEASED


def test_a_symbol_library_uploaded_as_a_footprint_is_held(tmp_path):
    rec = _up(tmp_path, _symlib(_symbol("X")), "footprint", "x.kicad_mod")
    assert rec.state == quarantine.HELD
    assert "not a KiCad footprint" in rec.reasons[0]


# ------------------------------------------------------------------------- 3D models


def test_a_step_model_lands_in_the_library_3dshapes(tmp_path):
    rec = _up(tmp_path, STEP, "3d_model", "PCM12 SMTBR.STEP")
    assert rec.state == quarantine.RELEASED, rec.reasons
    assert rec.placed == ["libraries/3dmodels/myproj.3dshapes/PCM12_SMTBR.step"]
    assert (tmp_path / rec.placed[0]).read_bytes() == STEP


@pytest.mark.parametrize("data, name", [(b"not a step", "x.step"), (b"ISO-10303-21;", "x.obj"), (STEP, "x.wrl")])
def test_a_file_that_is_not_a_model_is_held(tmp_path, data, name):
    rec = _up(tmp_path, data, "3d_model", name)
    assert rec.state == quarantine.HELD
    assert not (tmp_path / "libraries").exists()


# --------------------------------------------------------------------------- bundles


def _zip(entries: list[tuple[str, bytes]], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def test_a_vendor_bundle_is_routed_by_type_and_each_entry_has_its_own_verdict(tmp_path):
    bundle = _zip([
        ("PCM12SMTBR/KiCad/PCM12SMTBR.kicad_sym", _symlib(_symbol("PCM12SMTBR"))),
        ("PCM12SMTBR/KiCad/PCM12SMTBR.kicad_mod", _footprint("PCM12SMTBR")),
        ("PCM12SMTBR/3D/PCM12SMTBR.stp", STEP),
        ("PCM12SMTBR/datasheet.pdf", BENIGN_PDF),
        ("PCM12SMTBR/readme.txt", b"how to install\n"),
        ("PCM12SMTBR/Altium/PCM12SMTBR.SchLib", b"\x00binary"),
        ("PCM12SMTBR/KiCad/PCM12SMTBR.lib", b"EESchema-LIBRARY Version 2.4\n"),
        ("__MACOSX/._junk", b"x"),
    ])
    rec = _up(tmp_path, bundle, "kicad_bundle", "PCM12SMTBR.zip")
    assert rec.state == quarantine.RELEASED, rec.reasons
    by_name = {m["name"].split("/")[-1]: m for m in rec.members}

    assert by_name["PCM12SMTBR.kicad_sym"]["path"] == "libraries/symbols/myproj.kicad_sym"
    assert by_name["PCM12SMTBR.kicad_mod"]["path"] == "libraries/footprints/myproj.pretty/PCM12SMTBR.kicad_mod"
    assert by_name["PCM12SMTBR.stp"]["path"] == "libraries/3dmodels/myproj.3dshapes/PCM12SMTBR.stp"
    assert by_name["datasheet.pdf"]["path"] == "references/PCM12SMTBR/datasheet.pdf"
    assert by_name["readme.txt"]["path"] == "references/PCM12SMTBR/readme.txt"
    assert by_name["PCM12SMTBR.SchLib"]["state"] == "skipped"
    assert "KiCad 5 symbol library" in by_name["PCM12SMTBR.lib"]["reasons"][0]
    assert "._junk" not in by_name
    assert not (tmp_path / "libraries" / "PCM12SMTBR.SchLib").exists()

    # Each placed entry went through quarantine itself, with provenance.
    rows = {r["original_name"]: r for r in quarantine.ledger(tmp_path)}
    assert rows["PCM12SMTBR.zip:PCM12SMTBR/KiCad/PCM12SMTBR.kicad_mod"]["state"] == quarantine.RELEASED
    assert rows["PCM12SMTBR.zip:PCM12SMTBR/datasheet.pdf"]["inspection"]["state"] != quarantine.NOT_INSPECTED
    assert rows["PCM12SMTBR.zip"]["kind"] == "kicad_bundle"

    assert sr.resolve("myproj:PCM12SMTBR", project_dir=tmp_path, stock_root=_stock(tmp_path)).source == sr.CUSTOM
    assert sr.resolve_footprint(
        "myproj:PCM12SMTBR", project_dir=tmp_path, stock_root=_stock(tmp_path)
    ).source == sr.CUSTOM


def test_a_bundle_cannot_write_outside_where_it_is_routed(tmp_path):
    bundle = _zip(
        [
            ("../../escape.kicad_mod", _footprint("ESCAPE")),
            ("/etc/abs.kicad_mod", _footprint("ABS")),
            ("C:/win.kicad_mod", _footprint("WIN")),
            ("ok/fine.kicad_mod", _footprint("FINE")),
        ],
        symlink="link.kicad_mod",
    )
    rec = _up(tmp_path, bundle, "kicad_bundle", "b.zip")
    skipped = {m["name"]: m["reasons"][0] for m in rec.members if m["state"] == "skipped"}
    assert set(skipped) == {"../../escape.kicad_mod", "/etc/abs.kicad_mod", "C:/win.kicad_mod", "link.kicad_mod"}
    assert "symbolic link" in skipped["link.kicad_mod"]
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.kicad_mod"))
    assert all(p.startswith(("libraries/footprints/myproj.pretty/", "retrieved/")) for p in written), written
    assert not (tmp_path.parent / "escape.kicad_mod").exists()
    assert not any("ESCAPE" in p or "WIN" in p for p in written if p.startswith("libraries"))


def test_a_zip_bomb_is_not_unpacked(tmp_path, monkeypatch):
    monkeypatch.setattr(library_upload, "BUNDLE_MAX_RATIO", 10)
    bomb = _zip([("bomb.kicad_mod", b"(" + b" " * (3 * 1024 * 1024) + b")"), ("ok.kicad_mod", _footprint("OK"))])
    rec = _up(tmp_path, bomb, "kicad_bundle", "b.zip")
    by = {m["name"]: m for m in rec.members}
    assert by["bomb.kicad_mod"]["state"] == "skipped"
    assert "compressed suspiciously well" in by["bomb.kicad_mod"]["reasons"][0]
    assert by["ok.kicad_mod"]["state"] == quarantine.RELEASED


def test_a_bundle_over_the_limits_is_held_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(library_upload, "BUNDLE_MAX_ENTRIES", 3)
    many = _zip([(f"f{i}.kicad_mod", _footprint(f"F{i}")) for i in range(4)])
    rec = _up(tmp_path, many, "kicad_bundle", "many.zip")
    assert rec.state == quarantine.HELD
    assert "at most 3" in rec.reasons[0]
    assert not (tmp_path / "libraries").exists()

    monkeypatch.setattr(library_upload, "BUNDLE_MAX_ENTRIES", 200)
    monkeypatch.setattr(library_upload, "BUNDLE_MAX_TOTAL_BYTES", 100)
    rec = _up(tmp_path, _zip([("big.kicad_mod", _footprint("BIG") * 3)]), "kicad_bundle", "big.zip")
    assert rec.state == quarantine.HELD
    assert "unpacks to" in rec.reasons[0]


def test_something_that_is_not_a_zip_is_held(tmp_path):
    rec = _up(tmp_path, b"PK\x03\x04 not really", "kicad_bundle", "x.zip")
    assert rec.state == quarantine.HELD
    assert "not a readable zip" in rec.reasons[0]


def test_a_bundle_with_nothing_usable_is_held_and_says_why(tmp_path):
    rec = _up(tmp_path, _zip([("x.SchLib", b"\x00")]), "kicad_bundle", "altium.zip")
    assert rec.state == quarantine.HELD
    assert "nothing in it could be added" in rec.reasons[0]
    assert "x.SchLib" in rec.reasons[0]


def test_a_bundle_symbol_that_collides_is_held_while_the_rest_lands(tmp_path):
    _up(tmp_path, _symlib(_symbol("PART", pin="9")), "symbol", "mine.kicad_sym")
    bundle = _zip([("PART.kicad_sym", _symlib(_symbol("PART"))), ("PART.kicad_mod", _footprint("PART"))])
    rec = _up(tmp_path, bundle, "kicad_bundle", "vendor.zip")
    by = {m["name"]: m for m in rec.members}
    assert by["PART.kicad_sym"]["state"] == quarantine.HELD
    assert "already has a different PART" in by["PART.kicad_sym"]["reasons"][0]
    assert by["PART.kicad_mod"]["state"] == quarantine.RELEASED
