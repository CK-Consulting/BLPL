"""Where a project's files live, behind one interface.

The architecture wants each project on storage of its own with a hard size
limit, so one project filling the disk cannot stop every other. On this host
that is a btrfs subvolume per project with a qgroup limit; on XFS or ext4 it is a
project quota on a directory, on ZFS a dataset with a quota. All of them need
privilege the control plane must not have, so the enforcing implementation is
a separate single-purpose helper (docs/architecture-v2.md, "Storage") and lands
in a later phase.

What lands now is the interface and the one implementation that needs no
privilege: a named Docker volume. It isolates — a workspace sees its own volume
and no other, which the proxy checks — but it does not limit, and it says so
(``enforces_quota``) rather than accepting a quota it will ignore.
"""

from __future__ import annotations

from typing import Protocol

from .driver import Mount, WorkspaceKey

WORKSPACE_MOUNT = "/workspace"


class ProjectStorage(Protocol):
    enforces_quota: bool

    async def ensure(self, key: WorkspaceKey, *, quota_bytes: int | None = None) -> Mount:
        """The mount for this project's storage, creating it if need be."""
        ...

    async def usage(self, key: WorkspaceKey) -> int | None:
        """Bytes used, or None where the backend cannot measure it."""
        ...


class QuotaUnsupported(RuntimeError):
    pass


class VolumeStorage:
    """A named volume per project. Created by the driver on first ensure()."""

    enforces_quota = False

    async def ensure(self, key: WorkspaceKey, *, quota_bytes: int | None = None) -> Mount:
        if quota_bytes is not None:
            raise QuotaUnsupported(
                "Docker's local volume driver cannot limit a volume's size; "
                "use the quota-enforcing storage for a limit"
            )
        return Mount(kind="volume", source=key.volume_name(), target=WORKSPACE_MOUNT)

    async def usage(self, key: WorkspaceKey) -> int | None:
        return None
