"""Bind sources are checked on the disk, not by their spelling (Codex, PR #20).

A stage in a phase 2 or 3 workspace can replace its member worktree with a
symlink. The name still matches every lexical rule, and Docker follows the link
when it mounts, so the next run container would get wherever it points,
writable. Both layers now refuse that: the worker, building the run's mounts,
and the socket proxy, forwarding them.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.sandbox import jobs
from app.sandbox.docker import DockerConfig, build_create_body
from app.sandbox.policy import ProxyPolicy, Refused
from app.sandbox.proxy import DockerProxy, Request
from app.sandbox.realpath import NotReal, verify_beneath


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "projects"
    (root / "board" / ".git").mkdir(parents=True)
    member = root / ".worktrees" / "board" / "u9"
    member.mkdir(parents=True)
    (member / ".git").write_text("gitdir: x\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, member, outside


def test_verify_beneath(tree):
    root, member, outside = tree
    verify_beneath(str(root), str(member))
    verify_beneath(str(root), str(member / ".git"), final="file")
    with pytest.raises(NotReal, match="not a regular file"):
        verify_beneath(str(root), str(member), final="file")
    (root / ".worktrees" / "board" / "u7").symlink_to(outside)
    with pytest.raises(NotReal, match="symlink"):
        verify_beneath(str(root), str(root / ".worktrees" / "board" / "u7"))
    # A link in the middle of the path, not just at its end.
    (root / ".worktrees" / "board" / "u7").unlink()
    os.rename(root / ".worktrees" / "board", root / ".worktrees" / "real")
    (root / ".worktrees" / "board").symlink_to(root / ".worktrees" / "real")
    with pytest.raises(NotReal, match="symlink"):
        verify_beneath(str(root), str(root / ".worktrees" / "board" / "u9"))
    with pytest.raises(NotReal, match="not under"):
        verify_beneath(str(root), str(outside))


def _cfg(root: Path) -> jobs.WorkspaceRunConfig:
    return jobs.WorkspaceRunConfig(image="blpl-workspace:dev", projects_root=root,
                                   host_projects_root=str(root))


def test_the_worker_refuses_a_symlinked_member_worktree(tree):
    root, member, outside = tree
    jobs.run_spec_for(_cfg(root), "board", "r1")  # the real one is fine
    (root / ".worktrees" / "board" / "u7").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        jobs.run_spec_for(_cfg(root), "board", "r1")


def test_the_worker_refuses_a_symlinked_gitfile(tree):
    root, member, outside = tree
    (member / ".git").unlink()
    (outside / "gitfile").write_text("gitdir: /elsewhere\n")
    (member / ".git").symlink_to(outside / "gitfile")
    with pytest.raises(ValueError, match="symlink"):
        jobs.run_spec_for(_cfg(root), "board", "r1")


class _Up:
    async def get_json(self, path):
        return 404, None


def _decide(root: Path, body: dict, name: str, verify: bool = True):
    policy = ProxyPolicy(images=frozenset({"blpl-workspace:dev"}), networks=frozenset({"blpl-egress"}),
                         project_bind_roots=(str(root),), verify_paths=verify)
    return asyncio.run(DockerProxy(policy, _Up()).decide(
        Request("POST", "/containers/create", {"name": name}, json.dumps(body).encode())))


def test_the_proxy_refuses_a_symlinked_member_worktree_on_disk(tree):
    root, member, outside = tree
    spec = jobs.run_spec_for(_cfg(root), "board", "r1")
    body = build_create_body(spec, DockerConfig(network="blpl-egress"))
    _decide(root, body, spec.key.container_name())  # real: accepted
    # Built from the real tree, then the worktree swapped for a link before
    # the request reached the proxy: the spelling is unchanged.
    os.rename(member, root / ".worktrees" / "board" / "u9-real")
    member.symlink_to(outside)
    with pytest.raises(Refused, match="symlink"):
        _decide(root, body, spec.key.container_name())
    # Lexically the request is perfect; only the disk check sees it.
    _decide(root, body, spec.key.container_name(), verify=False)
