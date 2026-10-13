"""Proving a bind source is a real directory or file, not a symlink, on the host.

Docker resolves a bind mount's source on the host, and it follows symlinks.
A worktree directory that a stage replaced with a link would make the next run
container mount wherever the link points, read-write (Codex, PR #20). Checking
the path's spelling, as the proxy's rules do, cannot see that. So every
component from a trusted root down to the source is checked here:

- ``lstat`` it relative to the previous directory's descriptor; a link, or a
  file where a directory belongs, is refused;
- open it with ``O_NOFOLLOW``, and ``fstat`` the descriptor to confirm it is the
  same inode ``lstat`` saw, so a swap between the two is refused too.

This is the agent's ``open_at`` reasoning, applied on the host side, where
O_NOFOLLOW does hold (the gVisor caveat is about opens inside a runsc
sandbox). It narrows the window rather than closing it: Docker still opens the
path itself, later. Both the worker, which builds the mounts, and the socket
proxy, which forwards them, check, so a link has to appear in the gap between
the proxy's check and the daemon's mount.
"""

from __future__ import annotations

import os
import stat
from typing import Literal


class NotReal(ValueError):
    """Some component of the path is a link, missing, or the wrong kind."""


def verify_beneath(root: str, path: str, *, final: Literal["dir", "file", "any"] = "dir") -> None:
    """Raise NotReal unless every component of path below root is real.

    root itself is trusted (it is configuration), and path must lie beneath it
    or be it."""
    root = os.path.normpath(root)
    path = os.path.normpath(path)
    if path != root and not path.startswith(root + "/"):
        raise NotReal(f"{path} is not under {root}")
    parts = [p for p in path[len(root):].split("/") if p]
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise NotReal(f"cannot open {root}: {exc.strerror}") from None
    try:
        for i, name in enumerate(parts):
            last = i == len(parts) - 1
            want_dir = not last or final == "dir"
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise NotReal(f"{name} in {path} does not exist") from None
            if stat.S_ISLNK(before.st_mode):
                raise NotReal(f"{name} in {path} is a symlink")
            if want_dir and not stat.S_ISDIR(before.st_mode):
                raise NotReal(f"{name} in {path} is not a directory")
            if last and final == "file" and not stat.S_ISREG(before.st_mode):
                raise NotReal(f"{name} in {path} is not a regular file")
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if stat.S_ISDIR(before.st_mode) else 0)
            try:
                nfd = os.open(name, flags, dir_fd=fd)
            except OSError as exc:
                raise NotReal(f"{name} in {path} changed or cannot be opened: {exc.strerror}") from None
            after = os.fstat(nfd)
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                os.close(nfd)
                raise NotReal(f"{name} in {path} changed while it was checked")
            os.close(fd)
            fd = nfd
    finally:
        os.close(fd)
