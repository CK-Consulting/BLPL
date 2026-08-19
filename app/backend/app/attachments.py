"""Files a person hands to the design chat — datasheet pages, scope shots, a PDF.

Why this exists as its own store rather than base64 in the conversation:

A conversation is a JSONL file that is read whole, on every turn, to rebuild the
provider's message list. Inlining a 4 MB datasheet as base64 turns a 30 kB
transcript into a 30 MB one, and every subsequent turn pays to parse it again on
the way to sending it. So the bytes live beside the conversation and the
transcript keeps a reference: the history stays small and greppable, and the
image is read only when a turn is actually being assembled.

Content-addressed, so the same datasheet dropped into three conversations is
stored once, and re-dropping the file you dropped a minute ago is free.

The store sits under the project's ``.blpl/`` directory, which means sealing
picks it up with everything else — attachments are encrypted at rest exactly
like the design files they describe, with no separate code path to forget.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path


class AttachmentRejected(ValueError):
    """The upload cannot be stored. Carries a sentence meant for the user."""


# What the model layer can actually consume: llm_chat has an ImageBlock and a
# DocumentBlock and nothing else, so accepting a .docx here would only produce a
# file the assistant is structurally unable to look at.
#
# Extension is derived from the sniffed type, never from the name the browser
# sent — the name is untrusted and is kept only to show the user what they
# attached.
_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_DOC_TYPES = {"application/pdf": ".pdf"}
SUPPORTED_TYPES = {**_IMAGE_TYPES, **_DOC_TYPES}

# Anthropic caps a vision image around 25 MB and a PDF around 32 MB; other
# providers are stricter, not looser. Rejecting here costs a message, whereas
# letting it through costs a whole turn that fails at the provider after the
# upload has already been paid for.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024

# How many files may ride along on a single message. Not a resource limit — it
# is a "you meant to attach a folder" limit.
MAX_PER_MESSAGE = 10


@dataclass(frozen=True)
class Attachment:
    id: str            # sha256 of the content
    name: str          # what the user called it, for display only
    media_type: str
    bytes: int

    @property
    def kind(self) -> str:
        return "image" if self.media_type in _IMAGE_TYPES else "document"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "media_type": self.media_type,
            "bytes": self.bytes,
            "kind": self.kind,
        }


def sniff(data: bytes) -> str | None:
    """The media type the bytes actually are, or None if unsupported.

    Magic numbers, not the browser's Content-Type: a file picker will happily
    report image/png for anything renamed .png, and the provider decodes what is
    really there. Better to refuse it here with an explanation than to have a
    turn fail somewhere the user cannot see.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def limit_for(media_type: str) -> int:
    return MAX_IMAGE_BYTES if media_type in _IMAGE_TYPES else MAX_DOCUMENT_BYTES


def store_dir(conversations_dir: Path) -> Path:
    """Shared across every conversation in the project, not per-conversation.

    The same datasheet gets discussed in more than one conversation, and
    content-addressing only pays off if they share a namespace.
    """
    return Path(conversations_dir) / "attachments"


def save(conversations_dir: Path, name: str, data: bytes) -> Attachment:
    """Validate, sniff, and store. Returns the record to reference it by."""
    if not data:
        raise AttachmentRejected(f"{name}: file is empty")

    media_type = sniff(data)
    if media_type is None:
        raise AttachmentRejected(
            f"{name}: not a supported file. The assistant can read PNG, JPEG, "
            "GIF, WebP and PDF."
        )

    cap = limit_for(media_type)
    if len(data) > cap:
        raise AttachmentRejected(
            f"{name}: {len(data) // (1024 * 1024)} MB exceeds the "
            f"{cap // (1024 * 1024)} MB limit for {media_type}"
        )

    digest = hashlib.sha256(data).hexdigest()
    target = store_dir(conversations_dir) / f"{digest}{SUPPORTED_TYPES[media_type]}"
    _remember_name(target, Path(name).name)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # Same write-then-move discipline the rest of the app uses: a crash
        # mid-write must not leave a truncated file under a name that claims to
        # be the sha256 of its contents.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)

    return Attachment(
        id=digest,
        name=Path(name).name or "attachment",
        media_type=media_type,
        bytes=len(data),
    )


def path_of(conversations_dir: Path, attachment_id: str) -> Path | None:
    """Locate stored bytes by id, or None if this deploy has never seen them.

    The id is checked against the shape of a sha256 before it reaches the
    filesystem, so a caller cannot walk out of the store with a crafted id.
    """
    if len(attachment_id) != 64 or not all(c in "0123456789abcdef" for c in attachment_id):
        return None
    directory = store_dir(conversations_dir)
    for suffix in set(SUPPORTED_TYPES.values()):
        candidate = directory / f"{attachment_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _name_sidecar(blob: Path) -> Path:
    return blob.with_suffix(blob.suffix + ".name")


def _remember_name(blob: Path, original: str) -> None:
    """Keep the name the file arrived under, beside the bytes.

    Stored names are content hashes, so without this the only surviving name is
    ``<sha256>.pdf`` — which is what the model would be told the document is
    called. "Compare TPS62840.pdf against the design" is a question it can use;
    the same sentence with a hash in it is not.

    One small file per blob rather than one shared index: two uploads landing at
    once then need no locking and cannot corrupt each other's entry. First
    writer wins, matching the blob — the same bytes under a second name are the
    same document, and the name it was first introduced by is the honest one.
    """
    sidecar = _name_sidecar(blob)
    if sidecar.exists() or not original:
        return
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        # Explicit, not with_suffix(".tmp"): that resolves to the *blob's*
        # temp name, so two uploads of one file racing could move this text
        # over the bytes it describes.
        tmp = sidecar.with_name(sidecar.name + ".tmp")
        tmp.write_text(original, encoding="utf-8")
        tmp.replace(sidecar)
    except OSError:
        # A missing name costs the model a nicety; failing the upload over it
        # would cost the user their datasheet.
        pass


def name_of(path: Path) -> str:
    """What the file was called when it was uploaded, or its stored name.

    Falling back to the stored name keeps attachments saved before this existed
    working — they show a hash, which is what they always did.
    """
    try:
        remembered = _name_sidecar(path).read_text(encoding="utf-8").strip()
    except OSError:
        return path.name
    return remembered or path.name


def media_type_of(path: Path) -> str:
    """The stored type, from the name this module chose when it saved the file.

    Cheap and trustworthy in equal measure: the extension was derived from a
    magic-number sniff at save time, so re-reading the bytes to sniff them again
    would mean loading 25 MB off disk to re-learn something already known.
    """
    for media_type, suffix in SUPPORTED_TYPES.items():
        if path.suffix == suffix:
            return media_type
    return "application/octet-stream"


def read_b64(conversations_dir: Path, attachment_id: str) -> str | None:
    """The stored bytes, base64-encoded the way every provider wants them.

    None when the file is missing — an attachment can outlive its bytes if a
    project was restored from a partial backup, and that should cost the
    conversation one image rather than the ability to continue it.
    """
    path = path_of(conversations_dir, attachment_id)
    if path is None:
        return None
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
