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


def _legacy_part(data: Path, uid: int, mpn: str) -> None:
    """A record created before writes resolved to the stored spelling — the
    only way two names normalizing to one part number still coexist."""
    d = components.root(data, uid) / mpn
    d.mkdir(parents=True)
    components._git(d, "init", "-b", "main")
    components._git(d, "commit", "--allow-empty", "-m", f"Component {mpn}")


def test_colliding_exact_records_prefer_the_literal_spelling(data) -> None:
    """Legacy stores can hold ABC-123 and ABC123, both normalizing to the same
    part number. A caller taking the first exact hit must get the record
    spelled as asked — not whichever sorts first — and every hit must say the
    collision exists, because the records may hold different revisions."""
    _legacy_part(data, 1, "ABC-123")
    _legacy_part(data, 1, "ABC123")

    hits = components.find(data, 1, "ABC123")
    assert [h["mpn"] for h in hits] == ["ABC123", "ABC-123"]
    assert all(h["exact"] for h in hits)
    assert all("normalize to this part number" in h["why"] for h in hits)

    hits = components.find(data, 1, "ABC-123")
    assert [h["mpn"] for h in hits] == ["ABC-123", "ABC123"]


def test_a_collision_with_no_literal_spelling_asks_for_judgement(data) -> None:
    _legacy_part(data, 1, "ABC-123")
    _legacy_part(data, 1, "ABC123")
    hits = components.find(data, 1, "abc 123")
    assert len(hits) == 2 and all(h["exact"] for h in hits)
    assert "check which was meant" in hits[0]["why"]


def test_the_lookup_tool_surfaces_a_collision_instead_of_hiding_it(data, tmp_path) -> None:
    """library_lookup answers with one exact record. When several collide it has
    to be the literal one, and the rest have to be visible — silently returning
    an arbitrary record's documents is exactly the stale-data path."""
    import asyncio
    import json

    from app.agent.registry import _library_lookup
    from app.agent.toolspec import ToolContext
    from app.references import FilesystemSandbox, ReferenceManifest

    _legacy_part(data, 1, "ABC-123")
    _legacy_part(data, 1, "ABC123")

    class Library:
        def find(self, mpn):
            return components.find(data, 1, mpn)

        def documents(self, mpn):
            return components.documents(data, 1, mpn)

    ctx = ToolContext(
        project_id="dev05",
        project_dir=tmp_path,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="dev05", workspace_root=tmp_path)
        ),
        library=Library(),
    )
    body = json.loads(asyncio.run(_library_lookup(ctx, {"mpn": "ABC123"})))
    assert body["exact"]["mpn"] == "ABC123"
    assert [h["mpn"] for h in body["also_exact"]] == ["ABC-123"]
    assert "also_exact" in body["note"]


# -- one record per part, whatever the spelling -------------------------------


def test_a_variant_spelling_files_into_the_existing_record(data) -> None:
    """A write under a different spelling of a held part must land in that
    part's repository, not mint a second one — the split is exactly what makes
    later reads miss."""
    _add(data, 1, "ABC-123", "ds.pdf")
    _add(data, 1, "ABC123", "errata.pdf")
    held = components.parts(data, 1)
    assert [p["mpn"] for p in held] == ["ABC-123"]
    assert held[0]["documents"] == 2


def test_reads_under_a_variant_spelling_find_the_stored_record(data) -> None:
    """find() matches fuzzily, and then the caller reads with its own spelling.
    extraction() and documents() have to land on the record find() matched, or
    the advertised reuse never happens and the part is extracted again."""
    components.save_extraction(data, 1, "BQ27441DRZR-G1A", {"pinout": [{"pin": 1, "name": "VDD"}]})
    got = components.extraction(data, 1, "bq27441drzr g1a")
    assert got is not None and got["pinout"][0]["name"] == "VDD"
    names = [d["name"] for d in components.documents(data, 1, "bq27441drzr g1a")]
    assert names == ["extracted.json"]


def test_a_document_is_readable_by_the_name_it_was_reported_under(data) -> None:
    _add(data, 1, "X1", "ds.pdf", b"PDF BYTES")
    assert components.document_bytes(data, 1, "X1", "ds.pdf") == b"PDF BYTES"
    with pytest.raises(components.ComponentError):
        components.document_bytes(data, 1, "X1", ".git/config")


def test_extraction_stages_a_held_datasheet_instead_of_redownloading(data, tmp_path) -> None:
    """library_lookup promises an exact record's documents can be reused. When
    the record holds a PDF but no extraction, extract_datasheet_specs has to be
    able to read it — so it is copied into the project's datasheets/, where the
    resolver, the sandbox and the seal already govern it."""
    from app.agent.registry import _stage_library_documents
    from app.agent.toolspec import ToolContext
    from app.references import FilesystemSandbox, ReferenceManifest

    _add(data, 1, "PART9", "PART9-datasheet.pdf", b"PDF BYTES")
    components.add_document(data, 1, "PART9", "footprints/L.pretty/A.kicad_mod", b"(fp)")

    class Library:
        def documents(self, mpn):
            return components.documents(data, 1, mpn)

        def document(self, mpn, name):
            return components.document_bytes(data, 1, mpn, name)

    proj = tmp_path / "proj"
    proj.mkdir()
    ctx = ToolContext(
        project_id="dev06",
        project_dir=proj,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="dev06", workspace_root=proj)
        ),
        library=Library(),
    )
    assert _stage_library_documents(ctx, "PART9") == 1
    staged = proj / "datasheets" / "PART9" / "PART9-datasheet.pdf"
    assert staged.read_bytes() == b"PDF BYTES"
    # The footprint stays in the library — only top-level PDFs are datasheets.
    assert not (proj / "datasheets" / "PART9" / "footprints").exists()
    # Idempotent: a file already staged is not copied again.
    assert _stage_library_documents(ctx, "PART9") == 0


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


@pytest.mark.parametrize("name", [
    "footprints/Package_SON.pretty/archive/A.kicad_mod",
    "models/archive/A.step",
    "footprints/notes.txt",
    "footprints/L.pretty/A.step",
    "symbols/Lib/X.kicad_sym",
    "models/X.kicad_mod",
])
def test_a_path_kicad_cannot_resolve_is_refused(data, name) -> None:
    """The whole shape is enforced, not just the first segment. A file committed
    at footprints/L.pretty/archive/A.kicad_mod would be reported as held while
    KiCad's Lib:Name lookup cannot see it — accepted-but-unresolvable is worse
    than refused."""
    with pytest.raises(components.ComponentError, match="asset shape"):
        components.add_document(data, 1, "X1", name, b"x")


def test_replacing_a_document_in_a_subdirectory_is_a_revision(data) -> None:
    """The point of a part being a repository — the previous bytes stay readable
    at their commit rather than being overwritten."""
    rel = "footprints/L.pretty/A.kicad_mod"
    first = components.add_document(data, 1, "X1", rel, b"(fp v1)")
    second = components.add_document(data, 1, "X1", rel, b"(fp v2)")
    assert first["commit"] != second["commit"] and second["changed"]
