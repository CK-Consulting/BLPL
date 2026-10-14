"""What the Docker socket proxy permits, as pure functions.

Access to the Docker socket is root on the host: one ``POST /containers/create``
with ``Privileged`` or a bind of ``/`` and the caller owns the machine. So the
control plane never gets the socket. It gets the proxy, and the proxy forwards a
request only if it is one of a dozen shapes, aimed at a container this
deployment labelled, built from an image on a fixed list, with every field of
its body accounted for.

Why not tecnativa/docker-socket-proxy or one of its forks: they filter on the
URL — ``CONTAINERS=1``, ``EXEC=1``, ``POST=1`` — and never read the body. With
``CONTAINERS=1 POST=1`` a create carrying ``"Privileged": true`` and
``"Binds": ["/:/host"]`` passes, and nothing there can say "only containers
labelled ours" or "only this image". Those are exactly the restrictions that
matter, so this is a small purpose-built proxy instead.

Three rules carry most of the weight, and each answers a specific bypass:

* **Bodies are rebuilt, not forwarded.** Go's JSON decoder matches field names
  case-insensitively and takes the last of duplicate keys, so a body checked by
  a reader that differs on either point can say ``"privileged": true`` past a
  check for ``"Privileged"``. Every key must be one this module knows, with its
  exact spelling; an unknown key is refused unless its value is empty, and is
  then dropped. Duplicate keys, including ones that differ only in case, are
  refused outright. What reaches the daemon is ``json.dumps`` of what was
  checked, so it cannot hold anything that was not.
* **Paths are rebuilt, not forwarded.** A container named in a path is resolved
  to its full id by inspecting it, its labels are checked, and the forwarded
  request names the id. A rename between the check and the action cannot point
  the action somewhere else.
* **Unknown query parameters are refused**, not ignored: ``DELETE`` with
  ``v=1`` would take the project's volume with the container.

What this does not do: stop a compromised control plane from doing what the
control plane may legitimately do — start a workspace for any project and run
things in it. The control plane is the authority over projects by design. What
the proxy removes is the step from there to root on the host.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .driver import (
    AGENT_DIR,
    LABEL_CHECKOUT,
    LABEL_INSTANCE,
    LABEL_MANAGED,
    LABEL_PROJECT,
    LABEL_ROLE,
    GiB,
)


class Refused(Exception):
    """The request is outside policy. The message says which part."""


ROLE_NAME_PREFIX = {"workspace": "blpl-ws-", "desktop": "blpl-desk-", "run": "blpl-run-",
                    "sealer": "blpl-seal-"}
MIN_API_MINOR = 44


@dataclass(frozen=True)
class ProxyPolicy:
    instance: str = "blpl"
    # Exact references, ideally digest-pinned. A tag on this list is trusted
    # for whatever it points at, so the deployment pins what it builds.
    images: frozenset[str] = frozenset()
    networks: frozenset[str] = frozenset({"blpl-egress"})
    runtimes: frozenset[str] = frozenset()
    allow_default_runtime: bool = True
    # Host paths that may be bind-mounted, read-only, at or beneath. The shared
    # libraries, nothing else.
    ro_bind_roots: tuple[str, ...] = ()
    # Phase 2 storage: host directories holding project checkouts. A container
    # labelled with project P may bind, read-write, exactly <root>/P and
    # <root>/.worktrees/P, and nothing else beneath a root.
    project_bind_roots: tuple[str, ...] = ()
    # Phase 3: host directories holding one agent directory per container. A
    # container may bind exactly <root>/<its own name>, read-write, at
    # /run/blpl, and so reach no other workspace's socket or token.
    agent_dir_roots: tuple[str, ...] = ()
    # The sealed-blob directories (workspace.sealed_root, on the host). A
    # sealer, and nothing else, may bind one of these exactly, at /sealed.
    sealed_roots: tuple[str, ...] = ()
    # "name:ip" /etc/hosts entries a container may carry: the egress proxy
    # and its network's services, so a gVisor workspace can name them.
    extra_hosts: frozenset[str] = frozenset()
    # Check bind sources on the filesystem (realpath.verify_beneath), not just
    # by name. Needs the roots visible to the proxy at the same paths.
    verify_paths: bool = False
    # "run" (phase 4) is a container per pipeline run, confined exactly as a
    # workspace is, with the project's .git read-only.
    # "sealer" (phase 4) seals or unseals one project and exits; it alone may
    # mount the projects root, because sealing renames the project directory.
    roles: frozenset[str] = frozenset({"workspace", "run", "sealer"})
    volume_prefix: str = "blpl-ws-"
    max_memory: int = 16 * GiB
    max_nano_cpus: int = 8_000_000_000
    max_pids: int = 4096
    max_tmpfs_bytes: int = 4 * GiB
    default_api_version: str = "v1.44"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProxyPolicy":
        env = os.environ if env is None else env

        def words(name: str) -> list[str]:
            return [w for w in re.split(r"[\s,]+", env.get(name, "")) if w]

        d = cls()
        return cls(
            instance=env.get("BLPL_PROXY_INSTANCE", d.instance),
            images=frozenset(words("BLPL_PROXY_IMAGES")),
            networks=frozenset(words("BLPL_PROXY_NETWORKS")) or d.networks,
            runtimes=frozenset(words("BLPL_PROXY_RUNTIMES")),
            allow_default_runtime=env.get("BLPL_PROXY_REQUIRE_RUNTIME", "") != "1",
            ro_bind_roots=tuple(words("BLPL_PROXY_RO_BIND_ROOTS")),
            project_bind_roots=tuple(words("BLPL_PROXY_PROJECT_BIND_ROOTS")),
            agent_dir_roots=tuple(words("BLPL_PROXY_AGENT_DIR_ROOTS")),
            sealed_roots=tuple(words("BLPL_PROXY_SEALED_ROOTS")),
            extra_hosts=frozenset(words("BLPL_PROXY_EXTRA_HOSTS")),
            verify_paths=env.get("BLPL_PROXY_VERIFY_PATHS", "") == "1",
        )


# -- strict JSON ---------------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for k, _ in pairs:
        folded = k.casefold()
        if folded in seen:
            raise Refused(f"duplicate key {k!r} (keys are compared case-insensitively)")
        seen.add(folded)
    return dict(pairs)


def _no_constants(name: str) -> Any:
    raise Refused(f"{name} is not JSON")


def parse_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constants,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Refused(f"body is not JSON: {e}") from None
    if not isinstance(obj, dict):
        raise Refused("body must be a JSON object")
    return obj


# -- field validators ------------------------------------------------------------

Validator = Callable[[Any, str], Any]
_DROP = object()


def _is_zero(v: Any) -> bool:
    return v is None or v is False or v == 0 or v == "" or v == [] or v == {}


def _pick(obj: Any, allowed: Mapping[str, Validator], where: str,
          required: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise Refused(f"{where} must be an object")
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if k in allowed:
            got = allowed[k](v, f"{where}.{k}")
            if got is not _DROP:
                out[k] = got
        elif not _is_zero(v):
            raise Refused(f"{where}.{k} is not permitted")
    for k in required:
        if k not in out:
            raise Refused(f"{where}.{k} is required")
    return out


def _bool(v: Any, where: str) -> Any:
    if v is None:
        return _DROP  # unset, as a Go pointer field would be
    if not isinstance(v, bool):
        raise Refused(f"{where} must be a boolean")
    return v


def _must_be(expected: Any) -> Validator:
    def check(v: Any, where: str) -> Any:
        if v != expected or type(v) is not type(expected):
            raise Refused(f"{where} must be {json.dumps(expected)}")
        return v
    return check


def _int_range(lo: int, hi: int) -> Validator:
    def check(v: Any, where: str) -> Any:
        if v is None:
            return _DROP  # a required field then fails as missing
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise Refused(f"{where} must be an integer in [{lo}, {hi}]")
        return v
    return check


def _str_list(v: Any, where: str) -> Any:
    if v is None:
        return _DROP
    if not isinstance(v, list) or not all(isinstance(x, str) and "\0" not in x for x in v):
        raise Refused(f"{where} must be a list of strings")
    return v


def _nonempty_str_list(v: Any, where: str) -> Any:
    got = _str_list(v, where)
    if got is _DROP or not got:
        raise Refused(f"{where} must be a non-empty list of strings")
    return got


def _env_list(v: Any, where: str) -> Any:
    got = _str_list(v, where)
    if got is not _DROP and not all("=" in x and not x.startswith("=") for x in got):
        raise Refused(f"{where} entries must be NAME=value")
    return got


def _abs_path(v: Any, where: str) -> Any:
    if (
        not isinstance(v, str) or not v.startswith("/") or "\0" in v
        or posixpath.normpath(v) != v or ".." in v.split("/")
    ):
        raise Refused(f"{where} must be a normalised absolute path")
    return v


_NUMERIC_USER = re.compile(r"^(\d+)(?::(\d+))?$")


def _user(required: bool) -> Validator:
    def check(v: Any, where: str) -> Any:
        if v in ("", None) and not required:
            return _DROP
        m = _NUMERIC_USER.match(v) if isinstance(v, str) else None
        if not m:
            raise Refused(f"{where} must be a numeric uid[:gid]")
        if int(m.group(1)) == 0 or (m.group(2) is not None and int(m.group(2)) == 0):
            raise Refused(f"{where} may not be root")
        return v
    return check


def _str_map(v: Any, where: str) -> Any:
    if v is None:
        return _DROP
    if not isinstance(v, dict) or not all(
        isinstance(k, str) and isinstance(x, str) for k, x in v.items()
    ):
        raise Refused(f"{where} must map strings to strings")
    return v


def _regex(pattern: str) -> Validator:
    rx = re.compile(pattern)
    def check(v: Any, where: str) -> Any:
        if not isinstance(v, str) or not rx.fullmatch(v):
            raise Refused(f"{where} has a value outside policy")
        return v
    return check


def _one_of(values: frozenset[str] | set[str]) -> Validator:
    def check(v: Any, where: str) -> Any:
        if v not in values:
            raise Refused(f"{where} {v!r} is not permitted")
        return v
    return check


# -- labels ------------------------------------------------------------------------


def check_labels(policy: ProxyPolicy, labels: Any, where: str = "container") -> dict[str, str]:
    """A container's labels say it is this deployment's, in a permitted role."""
    if not isinstance(labels, dict):
        raise Refused(f"{where} has no labels")
    if labels.get(LABEL_MANAGED) != "true" or labels.get(LABEL_INSTANCE) != policy.instance:
        raise Refused(f"{where} is not managed by this deployment")
    if labels.get(LABEL_ROLE) not in policy.roles:
        raise Refused(f"{where} role {labels.get(LABEL_ROLE)!r} is not permitted")
    if not labels.get(LABEL_PROJECT):
        raise Refused(f"{where} has no project label")
    return labels


def check_volume_labels(policy: ProxyPolicy, labels: Any, project: str, name: str) -> None:
    if not isinstance(labels, dict) or labels.get(LABEL_MANAGED) != "true" \
            or labels.get(LABEL_INSTANCE) != policy.instance:
        raise Refused(f"volume {name} is not managed by this deployment")
    if labels.get(LABEL_PROJECT) != project:
        raise Refused(f"volume {name} belongs to a different project")


# -- container create -----------------------------------------------------------------


@dataclass(frozen=True)
class CreatePlan:
    body: dict[str, Any]
    name: str
    project: str
    volumes: tuple[str, ...]  # must exist and belong to `project`


def _under_root(path: str, roots: tuple[str, ...]) -> bool:
    for root in roots:
        root = root.rstrip("/") or "/"
        if root == "/":
            continue  # a root of "/" would permit everything; never honoured
        if path == root or path.startswith(root + "/"):
            return True
    return False


_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _project_dirs(policy: ProxyPolicy, project: str) -> set[str]:
    """The host directories a container for project may bind read-write."""
    if not _PROJECT_NAME.fullmatch(project):
        return set()
    out = set()
    for root in policy.project_bind_roots:
        root = root.rstrip("/")
        if root:
            out |= {f"{root}/{project}", f"{root}/.worktrees/{project}"}
    return out


def _member_dir(policy: ProxyPolicy, project: str, source: str) -> bool:
    """Whether source is one member's worktree of project:
    <root>/.worktrees/<project>/<member>. A run container mounts each one on
    its own (jobs.run_spec_for)."""
    if not _PROJECT_NAME.fullmatch(project):
        return False
    for root in policy.project_bind_roots:
        prefix = f"{root.rstrip('/')}/.worktrees/{project}/"
        if root.rstrip("/") and source.startswith(prefix):
            return bool(_PROJECT_NAME.fullmatch(source[len(prefix):]))
    return False


def _agent_dirs(policy: ProxyPolicy, container: str) -> set[str]:
    return {f"{r.rstrip('/')}/{container}" for r in policy.agent_dir_roots if r.rstrip("/")}


SEALER_TARGET = "/projects"
SEALED_TARGET = "/sealed"


def _root_of(path: str, roots: tuple[str, ...]) -> str:
    for r in roots:
        r = r.rstrip("/")
        if r and (path == r or path.startswith(r + "/")):
            return r
    raise Refused(f"{path} is under no configured root")


def _real(policy: ProxyPolicy, where: str, root: str, path: str, final: str) -> None:
    """The rules above read the source's spelling; this reads the filesystem.

    Docker follows symlinks in a bind source, so a worktree a stage swapped for
    a link would mount wherever the link points (Codex, PR #20). With
    verify_paths, every component from the configured root down is checked
    to be real, through the proxy's own read-only view of those roots at the
    same paths."""
    if not policy.verify_paths:
        return
    from .realpath import NotReal, verify_beneath

    try:
        verify_beneath(root, path, final=final)  # type: ignore[arg-type]
    except NotReal as exc:
        raise Refused(f"{where}.Source: {exc}") from None


def _run_checkout(policy: ProxyPolicy, project: str, checkout: str) -> set[str]:
    """The one checkout a run container may bind read-write: the owner's
    repository directory, or the named member's worktree (Codex, PR #20)."""
    if not _PROJECT_NAME.fullmatch(project):
        return set()
    out = set()
    for root in policy.project_bind_roots:
        root = root.rstrip("/")
        if not root:
            continue
        if checkout == "owner":
            out.add(f"{root}/{project}")
        elif re.fullmatch(r"u[0-9]{1,18}", checkout or ""):
            out.add(f"{root}/.worktrees/{project}/{checkout}")
    return out


def _mounts(policy: ProxyPolicy, v: Any, where: str, project: str = "",
            container: str = "", role: str = "", checkout: str = "") -> Any:
    if not isinstance(v, list):
        raise Refused(f"{where} must be a list")
    # A run container gets its invoker's checkout and nothing else writable,
    # and only that member's gitfile read-only.
    own = _run_checkout(policy, project, checkout) if role == "run" else set()
    out, targets = [], set()
    for i, m in enumerate(v):
        w = f"{where}[{i}]"
        kind = m.get("Type") if isinstance(m, dict) else None
        common: dict[str, Validator] = {"Type": _must_be(kind), "Target": _abs_path}
        if kind == "volume":
            got = _pick(m, {
                **common,
                "Source": _regex(re.escape(policy.volume_prefix) + r"[a-z0-9][a-z0-9_.-]{0,127}"),
                "ReadOnly": _bool,
                "VolumeOptions": lambda o, ww: _pick(o, {
                    "Subpath": _regex(r"(?!/)(?!.*(^|/)\.\.(/|$))[A-Za-z0-9_./-]{1,255}"),
                    "NoCopy": _bool,
                }, ww),
            }, w, required=("Source", "Target"))
        elif kind == "bind":
            got = _pick(m, {
                **common,
                "Source": _abs_path,
                "ReadOnly": _bool,
                "BindOptions": lambda o, ww: _pick(o, {
                    "Propagation": _one_of({"rprivate", "private"}),
                    "ReadOnlyForceRecursive": _bool,
                }, ww),
            }, w, required=("Source", "Target", "ReadOnly"))
            src = got["Source"]
            if got["ReadOnly"]:
                git_dirs = {d + "/.git" for d in _project_dirs(policy, project)
                            if "/.worktrees/" not in d}
                member_gitfile = (src.endswith("/.git")
                                  and _member_dir(policy, project, src[:-len("/.git")])
                                  and (role != "run" or src[:-len("/.git")] in own))
                if _under_root(src, policy.ro_bind_roots):
                    _real(policy, w, _root_of(src, policy.ro_bind_roots), src, "any")
                elif src in git_dirs:
                    _real(policy, w, _root_of(src, policy.project_bind_roots), src, "dir")
                elif member_gitfile:
                    _real(policy, w, _root_of(src, policy.project_bind_roots), src, "file")
                else:
                    raise Refused(f"{w}.Source {got['Source']} is not a permitted library root")
            elif role == "sealer" and got["Target"] == SEALER_TARGET and src in {
                    r.rstrip("/") for r in policy.project_bind_roots if r.rstrip("/")}:
                pass  # the projects root itself, for a sealer and nothing else
            elif role == "sealer" and got["Target"] == SEALED_TARGET and src in {
                    r.rstrip("/") for r in policy.sealed_roots if r.rstrip("/")}:
                pass  # the blob directory, which holds ciphertext only
            elif src in _agent_dirs(policy, container):
                if got["Target"] != AGENT_DIR:
                    raise Refused(f"{w}: an agent directory is mounted at {AGENT_DIR} only")
                _real(policy, w, _root_of(src, policy.agent_dir_roots), src, "dir")
            elif role == "run":
                if src not in own:
                    raise Refused(f"{w}: a run may bind only its invoker's checkout "
                                  f"({checkout or 'unnamed'}), not {src}")
                _real(policy, w, _root_of(src, policy.project_bind_roots), src, "dir")
            elif src in _project_dirs(policy, project) or _member_dir(policy, project, src):
                _real(policy, w, _root_of(src, policy.project_bind_roots), src, "dir")
            else:
                # Writable binds are the project's own checkouts and nothing
                # else: not a library, not a sibling project, not a parent.
                raise Refused(f"{w}: a writable bind must be this project's own directory "
                              f"(ReadOnly must be true for {got['Source']})")
        elif kind == "tmpfs":
            got = _pick(m, {
                **common,
                "TmpfsOptions": lambda o, ww: _pick(o, {
                    "SizeBytes": _int_range(1, policy.max_tmpfs_bytes),
                    "Mode": _int_range(0, 0o7777),
                }, ww, required=("SizeBytes",)),
            }, w, required=("Target", "TmpfsOptions"))
        else:
            raise Refused(f"{w}.Type {kind!r} is not permitted")
        if got["Target"] == "/":
            raise Refused(f"{w}.Target may not be /")
        if got["Target"] in targets:
            raise Refused(f"{w}.Target {got['Target']} is mounted twice")
        targets.add(got["Target"])
        out.append(got)
    return out


_NO_NEW_PRIVS = {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}


def _extra_hosts(policy: ProxyPolicy, v: Any, where: str) -> Any:
    got = _str_list(v, where)
    for entry in [] if got is _DROP else got:
        if entry not in policy.extra_hosts:
            # A host entry can point a name the workspace trusts (the egress
            # proxy) anywhere, so only the deployment's own are allowed.
            raise Refused(f"{where} entry {entry!r} is not permitted")
    return got


def _security_opt(v: Any, where: str) -> Any:
    got = _str_list(v, where)
    if got is _DROP or not any(x in _NO_NEW_PRIVS for x in got):
        raise Refused(f"{where} must set no-new-privileges")
    for x in got:
        # seccomp=unconfined, apparmor=unconfined, label=disable,
        # systempaths=unconfined: all loosen, none is needed.
        if x not in _NO_NEW_PRIVS:
            raise Refused(f"{where} {x!r} is not permitted")
    return got


def _cap_drop(v: Any, where: str) -> Any:
    got = _str_list(v, where)
    if got is _DROP or "ALL" not in got:
        raise Refused(f"{where} must drop ALL capabilities")
    return got


def check_create(policy: ProxyPolicy, query: Mapping[str, str], obj: Any) -> CreatePlan:
    q = _pick(dict(query), {"name": lambda v, w: v}, "query", required=("name",))
    name = q["name"]

    labels = check_labels(policy, (obj or {}).get("Labels"), "Labels")
    role = labels[LABEL_ROLE]
    prefix = ROLE_NAME_PREFIX[role]
    if not re.fullmatch(re.escape(prefix) + r"[a-z0-9][a-z0-9_.-]{0,100}", name):
        raise Refused(f"container name must start with {prefix}")

    host_allowed: dict[str, Validator] = {
        "Init": _bool,
        "ReadonlyRootfs": _must_be(True),
        "Privileged": _must_be(False),
        "CapDrop": _cap_drop,
        "SecurityOpt": _security_opt,
        "NetworkMode": _one_of(policy.networks),
        "Memory": _int_range(1, policy.max_memory),
        "MemorySwap": _int_range(1, policy.max_memory),
        "NanoCpus": _int_range(1, policy.max_nano_cpus),
        "PidsLimit": _int_range(1, policy.max_pids),
        "Mounts": lambda v, w: _mounts(policy, v, w, labels[LABEL_PROJECT], name,
                                       labels[LABEL_ROLE], labels.get(LABEL_CHECKOUT, "")),
        # Empty means the daemon's default runtime, decided below.
        "Runtime": lambda v, w: _DROP if v in ("", None) else _one_of(policy.runtimes)(v, w),
        "RestartPolicy": lambda v, w: _pick(v, {"Name": _one_of({"no", ""})}, w),
        "AutoRemove": _bool,
        "ExtraHosts": lambda v, w: _extra_hosts(policy, v, w),
        "LogConfig": lambda v, w: _pick(v, {
            "Type": _one_of({"json-file", "local"}), "Config": _str_map,
        }, w),
    }
    endpoint = lambda v, w: _pick(v, {"Aliases": _str_list}, w)  # noqa: E731
    body = _pick(obj, {
        "Image": _one_of(policy.images),
        "Cmd": _str_list,
        "Entrypoint": _str_list,
        "Env": _env_list,
        "Labels": _str_map,
        "User": _user(required=True),
        "WorkingDir": _abs_path,
        "Hostname": _regex(r"[a-z0-9][a-z0-9-]{0,62}"),
        "Tty": _bool,
        "AttachStdout": _bool,
        "AttachStderr": _bool,
        "StopSignal": _regex(r"SIG[A-Z0-9]+"),
        "StopTimeout": _int_range(0, 600),
        "HostConfig": lambda v, w: _pick(v, host_allowed, w, required=(
            "ReadonlyRootfs", "CapDrop", "SecurityOpt", "NetworkMode",
            "Memory", "MemorySwap", "NanoCpus", "PidsLimit",
        )),
        "NetworkingConfig": lambda v, w: _pick(v, {
            "EndpointsConfig": lambda e, ww: {
                k: endpoint(x, f"{ww}.{k}") for k, x in _endpoints(policy, e, ww).items()
            },
        }, w),
    }, "body", required=("Image", "User", "Labels", "HostConfig"))

    host = body["HostConfig"]
    if host["MemorySwap"] != host["Memory"]:
        raise Refused("HostConfig.MemorySwap must equal Memory (no swap)")
    if "Runtime" not in host and not policy.allow_default_runtime:
        raise Refused(f"HostConfig.Runtime is required (one of {sorted(policy.runtimes)})")

    volumes = tuple(
        m["Source"] for m in host.get("Mounts", []) if m["Type"] == "volume"
    )
    return CreatePlan(body=body, name=name, project=labels[LABEL_PROJECT], volumes=volumes)


def _endpoints(policy: ProxyPolicy, v: Any, where: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise Refused(f"{where} must be an object")
    for k in v:
        if k not in policy.networks:
            raise Refused(f"{where}: network {k!r} is not permitted")
    return v


# -- exec, volumes, listing ----------------------------------------------------------------


def check_exec_create(obj: Any) -> dict[str, Any]:
    return _pick(obj, {
        "Cmd": _nonempty_str_list,
        "Env": _env_list,
        "WorkingDir": _abs_path,
        "User": _user(required=False),
        "AttachStdout": _bool,
        "AttachStderr": _bool,
        "Tty": _bool,
    }, "exec", required=("Cmd",))


def check_exec_start(obj: Any) -> dict[str, Any]:
    return _pick(obj, {"Detach": _bool, "Tty": _bool}, "exec start")


def check_volume_create(policy: ProxyPolicy, obj: Any) -> dict[str, Any]:
    body = _pick(obj, {
        "Name": _regex(re.escape(policy.volume_prefix) + r"[a-z0-9][a-z0-9_.-]{0,127}"),
        "Driver": _one_of({"local"}),
        "Labels": _str_map,
    }, "volume", required=("Name", "Labels"))
    labels = body["Labels"]
    if labels.get(LABEL_MANAGED) != "true" or labels.get(LABEL_INSTANCE) != policy.instance \
            or not labels.get(LABEL_PROJECT):
        raise Refused("volume must carry this deployment's labels and a project")
    return body


def force_list_filters(policy: ProxyPolicy, raw: str | None) -> str:
    """The client's filters, narrowed to this deployment's containers.

    Docker ANDs label filters, so adding ours can only remove rows.
    """
    filters: dict[str, Any] = {}
    if raw:
        try:
            filters = json.loads(raw, object_pairs_hook=_no_duplicates)
        except json.JSONDecodeError:
            raise Refused("filters is not JSON") from None
        if not isinstance(filters, dict):
            raise Refused("filters must be an object")
    labels = filters.get("label", [])
    if isinstance(labels, dict):  # the legacy {"k=v": true} form
        labels = [k for k, on in labels.items() if on]
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise Refused("filters.label must be a list of strings")
    filters["label"] = sorted(set(labels) | {
        f"{LABEL_MANAGED}=true", f"{LABEL_INSTANCE}={policy.instance}",
    })
    return json.dumps(filters, sort_keys=True)


# -- routing -------------------------------------------------------------------------------

_ID = r"(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})"
_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"/_ping"), "ping"),
    ("HEAD", re.compile(r"/_ping"), "ping"),
    ("GET", re.compile(r"/version"), "version"),
    ("GET", re.compile(r"/containers/json"), "list"),
    ("POST", re.compile(r"/containers/create"), "create"),
    ("POST", re.compile(rf"/containers/{_ID}/(?P<op>start|stop|pause|unpause)"), "op"),
    ("GET", re.compile(rf"/containers/{_ID}/json"), "inspect"),
    ("GET", re.compile(rf"/containers/{_ID}/logs"), "logs"),
    ("DELETE", re.compile(rf"/containers/{_ID}"), "remove"),
    ("POST", re.compile(rf"/containers/{_ID}/exec"), "exec_create"),
    ("POST", re.compile(rf"/exec/{_ID}/start"), "exec_start"),
    ("GET", re.compile(rf"/exec/{_ID}/json"), "exec_inspect"),
    ("POST", re.compile(r"/volumes/create"), "volume_create"),
    ("GET", re.compile(rf"/volumes/{_ID}"), "volume_inspect"),
]
_VERSION = re.compile(r"^/v1\.(?P<minor>\d{1,3})(?P<rest>/.*)$")


@dataclass(frozen=True)
class Route:
    action: str
    version: str           # "v1.NN", to forward with
    ident: str | None = None
    op: str | None = None


def route(policy: ProxyPolicy, method: str, path: str) -> Route:
    version = policy.default_api_version
    m = _VERSION.match(path)
    if m:
        if int(m.group("minor")) < MIN_API_MINOR:
            raise Refused(f"API version 1.{m.group('minor')} is older than 1.{MIN_API_MINOR}")
        version, path = f"v1.{int(m.group('minor'))}", m.group("rest")
    for meth, rx, action in _ROUTES:
        hit = rx.fullmatch(path)
        if hit and meth == method:
            groups = hit.groupdict()
            return Route(action, version, groups.get("id"), groups.get("op"))
    raise Refused(f"{method} {path} is not permitted")


def check_query(action: str, query: Mapping[str, str]) -> dict[str, str]:
    """The query parameters each action may carry, everything else refused."""
    def flag(v: Any, w: str) -> Any:
        if v not in ("0", "1", "true", "false"):
            raise Refused(f"{w} must be a boolean")
        return v

    def off(v: Any, w: str) -> Any:
        if v not in ("0", "false"):
            raise Refused(f"{w} is not permitted")
        return _DROP

    allowed: dict[str, dict[str, Validator]] = {
        "list": {"all": flag, "limit": _regex(r"\d{1,4}"), "size": flag,
                 "filters": lambda v, w: v},
        "op": {"t": _regex(r"\d{1,4}"), "signal": _regex(r"SIG[A-Z0-9]+")},
        "remove": {"force": flag, "v": off, "link": off},
        "logs": {"follow": flag, "stdout": flag, "stderr": flag},
        "create": {"name": lambda v, w: v},
    }
    q = dict(query)
    spec = allowed.get(action, {})
    for k in q:
        if k not in spec:
            raise Refused(f"query parameter {k!r} is not permitted here")
    return _pick(q, spec, "query")
