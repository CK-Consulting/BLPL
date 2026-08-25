"""Asking the library before spending a distributor call or an extraction.

The library is scoped to the user, and that scoping is the whole of the answer
to "what about a datasheet under NDA" (docs/component-library.md). So the two
things tested hardest here are that the lookup is genuinely useful — it finds a
part spelled differently — and that it never quietly hands back a *different*
orderable part as though it were the one asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import components


@pytest.fixture()
def data(tmp_path: Path) -> Path:
    return tmp_path


def _add(data: Path, uid: int, mpn: str, name: str = "datasheet.pdf", body: bytes = b"PDF") -> None:
    components.add_document(data, uid, mpn, name, body)


# -- finding ------------------------------------------------------------------


def test_an_exact_part_is_found_and_marked_exact(data) -> None:
    _add(data, 1, "BQ27441DRZR-G1A")
    hits = components.find(data, 1, "BQ27441DRZR-G1A")
    assert len(hits) == 1 and hits[0]["exact"]


def test_the_same_part_spelled_differently_still_matches(data) -> None:
    """Suppliers disagree about case and punctuation for one orderable part."""
    _add(data, 1, "BQ27441DRZR-G1A")
    hits = components.find(data, 1, "bq27441drzr g1a")
    assert len(hits) == 1 and hits[0]["exact"]


def test_a_variant_comes_back_labelled_rather_than_as_the_answer(data) -> None:
    """The value and the danger are the same lookup.

    The suffix that differs between orderable variants is usually package,
    temperature grade or reel — which is exactly what a footprint and a pin map
    depend on. Handing one back as the other is a plausible, wrong board.
    """
    _add(data, 1, "BQ27441DRZR-G1A")
    hits = components.find(data, 1, "BQ27441DRZT-G1B")

    assert len(hits) == 1
    assert hits[0]["exact"] is False
    assert "different orderable part" in hits[0]["why"]


def test_two_unrelated_parts_do_not_match(data) -> None:
    _add(data, 1, "BQ27441DRZR-G1A")
    assert components.find(data, 1, "STM32U5G9NJH6Q") == []


def test_a_short_shared_prefix_is_not_a_match(data) -> None:
    """Every part a manufacturer makes shares its first characters."""
    _add(data, 1, "BQ27441DRZR-G1A")
    assert components.find(data, 1, "BQ25895RTWR") == []


def test_one_users_library_is_invisible_to_another(data) -> None:
    """The scoping that docs/component-library.md rests its NDA position on."""
    _add(data, 1, "BQ27441DRZR-G1A")
    assert components.find(data, 2, "BQ27441DRZR-G1A") == []


# -- what a part may hold -----------------------------------------------------


def test_a_footprint_keeps_the_library_name_in_its_path(data) -> None:
    """KiCad resolves Package_SON:X by looking in Package_SON.pretty/X.kicad_mod.
    Flattening that would throw away the half of the reference saying which
    library, so the shape has to survive storage."""
    components.add_document(
        data, 1, "BQ27441DRZR-G1A",
        "footprints/Package_SON.pretty/Texas_VSON-12.kicad_mod", b"(footprint)",
    )
    names = [d["name"] for d in components.documents(data, 1, "BQ27441DRZR-G1A")]
    assert "footprints/Package_SON.pretty/Texas_VSON-12.kicad_mod" in names


def test_symbols_and_models_are_held_too(data) -> None:
    components.add_document(data, 1, "X1", "symbols/Lib.kicad_symdir/X.kicad_sym", b"(sym)")
    components.add_document(data, 1, "X1", "models/X.step", b"solid")
    names = {d["name"] for d in components.documents(data, 1, "X1")}
    assert names == {"symbols/Lib.kicad_symdir/X.kicad_sym", "models/X.step"}


def test_a_loose_datasheet_still_sits_at_the_top(data) -> None:
    _add(data, 1, "X1", "ds.pdf")
    assert [d["name"] for d in components.documents(data, 1, "X1")] == ["ds.pdf"]


def test_the_part_count_sees_documents_in_subdirectories(data) -> None:
    _add(data, 1, "X1", "ds.pdf")
    components.add_document(data, 1, "X1", "footprints/L.pretty/A.kicad_mod", b"(fp)")
    assert components.parts(data, 1)[0]["documents"] == 2


# -- containment --------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "../escape.pdf", "footprints/../../escape.pdf", "/etc/passwd", ".git/config", ".hidden",
])
def test_a_document_cannot_escape_its_part(data, name) -> None:
    with pytest.raises(components.ComponentError):
        components.add_document(data, 1, "X1", name, b"x")


def test_an_unlisted_subdirectory_is_refused(data) -> None:
    """A small allowlist, not arbitrary nesting: enough shape to hold what KiCad
    needs and nothing else."""
    with pytest.raises(components.ComponentError, match="bare filename or sit under"):
        components.add_document(data, 1, "X1", "secrets/thing.pdf", b"x")


def test_replacing_a_document_in_a_subdirectory_is_a_revision(data) -> None:
    """The point of a part being a repository — the previous bytes stay readable
    at their commit rather than being overwritten."""
    rel = "footprints/L.pretty/A.kicad_mod"
    first = components.add_document(data, 1, "X1", rel, b"(fp v1)")
    second = components.add_document(data, 1, "X1", rel, b"(fp v2)")
    assert first["commit"] != second["commit"] and second["changed"]
