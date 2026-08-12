"""Turn a browser upload into a clean set of project files.

The third way into the app, next to clone and init: you have a design folder on
whatever machine you are sitting at, and it needs to become a git-backed working
copy on the server. The browser can only hand us file contents (never paths), so
the upload arrives as a flat list of (filename, bytes) — where filename may carry
a relative path if the client sent one, or the whole thing may be a single .zip
of the project folder.

Everything here is pure functions over bytes, so the policy is unit-testable
without a server: which names are acceptable, which files are design content,
and how an uploaded folder collapses onto the project root. The route in main.py
owns the filesystem and git side.

Policy, stated once:

- Nothing is discarded silently. A file we refuse comes back in ``skipped`` with
  a reason — the same no-silent-drops rule Stage 0 learned the hard way.
- Path components may not be ``..``, absolute, or dot-prefixed. That keeps the
  upload inside the project, and keeps ``.git`` / ``.blpl`` — directories the
  app owns — out of an import's hands.
- Only design-shaped suffixes are imported (markdown, config, data tables,
  datasheets, images, KiCad files). An executable or unknown binary in the
  upload is skipped with a reason, not written into a tree that stage
  subprocesses run over.
- A single common top-level folder is stripped, recursively. Zipping a project
  *folder* (or selecting one) is the natural gesture, but the pipeline reads
  ``*.md`` from the project root — without the strip every import would land one
  directory too deep and Stage 0 would see nothing.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_FILES = 2000
MAX_TOTAL_BYTES = 100 * 1024 * 1024  # decompressed, across the whole upload

ALLOWED_SUFFIXES = {
    # design inputs
    ".md", ".markdown", ".txt", ".rst",
    # config and structured data
    ".yaml", ".yml", ".json", ".toml", ".csv", ".tsv",
    # references
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    # KiCad projects and libraries
    ".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_wks",
    ".kicad_sym", ".kicad_mod", ".net", ".lib",
    # fab outputs someone may want to keep with the design
    ".gbr", ".drl", ".pos", ".step", ".stp", ".wrl",
}


class ImportRejected(ValueError):
    """The upload as a whole cannot be accepted (too big, too many files,
    unreadable zip). Distinct from a single file being skipped."""


@dataclass(frozen=True)
class ImportFile:
    path: PurePosixPath
    data: bytes


@dataclass(frozen=True)
class SkippedFile:
    name: str
    reason: str


def collect(uploads: list[tuple[str, bytes]]) -> tuple[list[ImportFile], list[SkippedFile]]:
    """Uploads in, importable files + skip report out.

    A single .zip upload is treated as a container and expanded; anything else
    is taken file-by-file. Either way the same per-path rules apply, so a zip
    cannot smuggle in what a plain upload could not.
    """
    if len(uploads) == 1 and uploads[0][0].lower().endswith(".zip"):
        entries = _zip_entries(uploads[0][1])
    else:
        entries = uploads

    if len(entries) > MAX_FILES:
        raise ImportRejected(f"upload has {len(entries)} files; the limit is {MAX_FILES}")

    total = 0
    kept: dict[PurePosixPath, bytes] = {}
    skipped: list[SkippedFile] = []
    for raw_name, data in entries:
        path = _safe_relpath(raw_name)
        if path is None:
            skipped.append(SkippedFile(raw_name, "unsafe path (absolute, '..', or dot-prefixed)"))
            continue
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            skipped.append(SkippedFile(raw_name, f"suffix {path.suffix or '(none)'!r} is not a design file"))
            continue
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ImportRejected(
                f"upload exceeds {MAX_TOTAL_BYTES // (1024 * 1024)} MB decompressed"
            )
        kept[path] = data  # duplicate paths: the later entry wins, like extraction would

    return _strip_common_root(kept), skipped


def _zip_entries(blob: bytes) -> list[tuple[str, bytes]]:
    """Expand a zip defensively: entry count and decompressed size are checked
    against the same caps as a plain upload *before* anything is inflated, so a
    zip bomb fails on its declared sizes, not after filling memory."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ImportRejected(f"not a readable zip: {exc}")

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES:
        raise ImportRejected(f"zip has {len(infos)} files; the limit is {MAX_FILES}")
    declared = sum(i.file_size for i in infos)
    if declared > MAX_TOTAL_BYTES:
        raise ImportRejected(
            f"zip decompresses to {declared // (1024 * 1024)} MB; "
            f"the limit is {MAX_TOTAL_BYTES // (1024 * 1024)} MB"
        )
    return [(i.filename, zf.read(i)) for i in infos]


def _safe_relpath(raw: str) -> PurePosixPath | None:
    """A client-supplied name, held to the rules that keep it inside the project.

    Backslashes are treated as separators (a Windows browser or zip tool may
    send them), empty and ``.`` components collapse, and any component that is
    ``..`` or dot-prefixed disqualifies the whole name — dot-prefixed because
    ``.git`` and ``.blpl`` belong to the app, not to an upload.
    """
    parts = [c for c in raw.replace("\\", "/").split("/") if c not in ("", ".")]
    if not parts or any(c == ".." or c.startswith(".") for c in parts):
        return None
    return PurePosixPath(*parts)


def _strip_common_root(files: dict[PurePosixPath, bytes]) -> list[ImportFile]:
    """Collapse ``myboard/design.md`` → ``design.md`` when *every* file shares
    the one top-level folder, repeating for nested wrappers (zip-in-a-folder).
    The pipeline reads ``*.md`` non-recursively from the project root, so an
    un-stripped folder upload would import fine and then build nothing."""
    paths = dict(files)
    while paths:
        firsts = {p.parts[0] for p in paths}
        if len(firsts) != 1 or any(len(p.parts) == 1 for p in paths):
            break
        paths = {PurePosixPath(*p.parts[1:]): data for p, data in paths.items()}
    return [ImportFile(path=p, data=d) for p, d in sorted(paths.items())]
