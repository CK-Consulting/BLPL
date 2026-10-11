"""What an attached file is called by the time the model sees it.

Attachments are stored by content hash, which is right for storage — the same
datasheet discussed in three conversations is one file on disk. It is wrong for
naming: `a3f9…e1.pdf` tells the model nothing, while "check TPS62840.pdf against
the design" is a sentence it can act on.

The name therefore lives beside the bytes, one small file per blob. A shared
index would need locking to survive two uploads landing at once; a per-blob
sidecar cannot be corrupted by a concurrent write, because two uploads of
different files never touch the same one.
"""

from __future__ import annotations

import pytest

from app import attachments

PDF = b"%PDF-1.4\n" + b"stand-in for a datasheet\n" * 8
OTHER_PDF = b"%PDF-1.4\n" + b"a different part entirely\n" * 8


def test_the_uploaded_name_survives_to_the_message(tmp_path):
    """Without this the model is told the document is called `<sha256>.pdf`."""
    rec = attachments.save(tmp_path, "TPS62840.pdf", PDF)
    path = attachments.path_of(tmp_path, rec.id)

    assert path.name.startswith(rec.id)          # stored by content, as before
    assert attachments.name_of(path) == "TPS62840.pdf"


def test_a_directory_in_the_name_is_not_carried_along(tmp_path):
    """Browsers send a bare filename, but a scripted client need not."""
    rec = attachments.save(tmp_path, "../../etc/passwd.pdf", PDF)
    assert attachments.name_of(attachments.path_of(tmp_path, rec.id)) == "passwd.pdf"


def test_an_attachment_stored_before_names_existed_still_reads(tmp_path):
    """Older uploads have no sidecar. They show a hash — which is what they
    always showed — rather than failing to resolve."""
    rec = attachments.save(tmp_path, "datasheet.pdf", PDF)
    path = attachments.path_of(tmp_path, rec.id)
    path.with_name(path.name + ".name").unlink()

    assert attachments.name_of(path) == path.name


def test_the_first_name_wins_for_identical_bytes(tmp_path):
    """The same document uploaded twice is one document. Renaming it under the
    second upload would rename it inside the first conversation too, which is
    not something the person doing the uploading asked for."""
    first = attachments.save(tmp_path, "TPS62840.pdf", PDF)
    second = attachments.save(tmp_path, "untitled(3).pdf", PDF)

    assert first.id == second.id
    assert attachments.name_of(attachments.path_of(tmp_path, first.id)) == "TPS62840.pdf"


def test_the_sidecar_never_lands_on_the_blob(tmp_path):
    """The sidecar's temp name used to resolve to the blob's temp name, so two
    uploads racing could move a few bytes of filename over the attachment
    itself. Different files must not share a scratch name."""
    a = attachments.save(tmp_path, "one.pdf", PDF)
    b = attachments.save(tmp_path, "two.pdf", OTHER_PDF)

    assert attachments.path_of(tmp_path, a.id).read_bytes() == PDF
    assert attachments.path_of(tmp_path, b.id).read_bytes() == OTHER_PDF
    assert not list(attachments.store_dir(tmp_path).glob("*.tmp"))


def test_path_of_does_not_hand_back_the_sidecar(tmp_path):
    """It looks up `{digest}{suffix}`; a name file sitting beside it must not
    be mistaken for the bytes."""
    rec = attachments.save(tmp_path, "datasheet.pdf", PDF)
    assert attachments.path_of(tmp_path, rec.id).read_bytes() == PDF


def test_a_read_only_store_costs_the_name_not_the_upload(tmp_path, monkeypatch):
    """A missing name is a nicety lost; failing the upload over it would cost
    the user their datasheet."""
    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(attachments.Path, "write_text", boom)
    rec = attachments.save(tmp_path, "TPS62840.pdf", PDF)
    assert attachments.path_of(tmp_path, rec.id).read_bytes() == PDF
