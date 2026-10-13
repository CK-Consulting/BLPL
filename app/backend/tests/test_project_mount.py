"""BLPL_PROJECT_MOUNT=absent has to be true of the container, not just said.

Compose passed the flag nowhere and bound all of ./data into the backend
anyway (Codex, PR #20), so a backend "without the project mount" could read
every project. These check the startup refusal that makes the flag mean
something, against mount tables written the way the kernel writes them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from app import workspace_files as wf  # noqa: E402


def _mountinfo(root: str, *, mask: bool, data_fs: str = "ext4") -> str:
    data = root.rsplit("/", 1)[0]
    lines = [
        "1680 1600 0:40 / / rw,relatime - overlay overlay rw,lowerdir=/x",
        f"1724 1680 259:2 /srv/blpl/app/data {data} rw,noatime - {data_fs} /dev/nvme0n1p2 rw",
    ]
    if mask:
        lines.append(f"1755 1724 0:202 / {root} ro,nosuid,nodev,noexec - tmpfs tmpfs rw,size=1024k,mode=555")
    return "\n".join(lines) + "\n"


@pytest.fixture
def root(tmp_path, monkeypatch):
    for name in ("BLPL_PROJECT_MOUNT", "BLPL_FILES_VIA_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    r = tmp_path / "data" / "projects"
    r.mkdir(parents=True)
    return r


def test_a_masked_projects_directory_is_not_visible(root):
    assert not wf.projects_visible(root, _mountinfo(str(root), mask=True))
    # The ./data bind alone: visible even while empty, since the next project
    # lands there; and visible whatever the host filesystem under it is.
    assert wf.projects_visible(root, _mountinfo(str(root), mask=False))
    assert wf.projects_visible(root, _mountinfo(str(root), mask=False, data_fs="tmpfs"))
    # Anything in it is visible, whatever is mounted there.
    (root / ".sealed").mkdir()
    assert wf.projects_visible(root, _mountinfo(str(root), mask=True))


def test_a_bound_host_directory_on_a_tmpfs_is_not_mistaken_for_the_mask(root):
    info = _mountinfo(str(root), mask=False) + (
        f"1800 1724 0:57 /srv/projects {root} rw - tmpfs tmpfs rw\n")
    assert wf.projects_visible(root, info)


def test_an_escaped_mount_point_is_read_as_the_kernel_wrote_it(tmp_path):
    r = tmp_path / "my data" / "projects"
    r.mkdir(parents=True)
    escaped = str(r).replace(" ", "\\040")
    info = f"1 0 0:40 / / rw - overlay overlay rw\n9 1 0:9 / {escaped} ro - tmpfs tmpfs rw\n"
    assert not wf.projects_visible(r, info)


def test_absent_while_the_projects_are_visible_refuses_to_start(root, monkeypatch):
    monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")
    monkeypatch.setenv("BLPL_FILES_VIA_WORKSPACE", "1")
    with pytest.raises(wf.MountMisconfigured, match="visible"):
        wf.check_project_mount(root, mountinfo=_mountinfo(str(root), mask=False))
    assert wf.check_project_mount(root, mountinfo=_mountinfo(str(root), mask=True)) is None


def test_absent_without_the_agent_refuses_to_start(root, monkeypatch):
    monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")
    with pytest.raises(wf.MountMisconfigured, match="BLPL_FILES_VIA_WORKSPACE"):
        wf.check_project_mount(root, mountinfo=_mountinfo(str(root), mask=True))


def test_a_missing_mount_the_backend_expects_is_reported(root):
    warning = wf.check_project_mount(root, mountinfo=_mountinfo(str(root), mask=True))
    assert warning and "BLPL_PROJECT_MOUNT" in warning
    assert wf.check_project_mount(root, mountinfo=_mountinfo(str(root), mask=False)) is None


def test_the_backend_does_not_start_when_the_flag_and_the_mounts_disagree(client, monkeypatch):
    """Through the real startup hook: the projects root the fixture made is an
    ordinary directory with a project in it, so it is visible."""
    from starlette.testclient import TestClient

    import app.main as main

    (main.PROJECTS_ROOT / "board").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")
    monkeypatch.setenv("BLPL_FILES_VIA_WORKSPACE", "1")
    with pytest.raises(wf.MountMisconfigured):
        with TestClient(main.app):
            pass
