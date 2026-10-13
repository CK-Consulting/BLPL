"""The Docker socket proxy: the only process that holds the real socket.

Runs as its own container with ``/var/run/docker.sock`` mounted and nothing else
— no database, no project files, no network beyond the Unix socket it listens
on. The control plane is given that listening socket instead of the daemon's.
What it permits is ``policy.py``; this module is the plumbing around it.

One request per connection, by design. A request is read whole (bounded),
decided, and either refused with a 403 or rewritten and sent upstream on a
fresh connection with ``Connection: close``, after which the upstream's bytes
are copied back verbatim until it closes. That last part is what makes exec
output work without understanding it: the daemon answers ``exec start`` by
streaming the process's output on the same connection until the process exits,
and a byte copy carries that as faithfully as it carries a JSON reply. Nothing
is ever copied from the client to the daemon after the checked request, so
there is no stdin and no connection upgrade to reason about — and none is
needed, because nothing the control plane runs reads stdin.

Run with:  python -m app.sandbox.proxy
  BLPL_PROXY_LISTEN    socket to listen on (default /run/blpl-docker-proxy/docker.sock)
  BLPL_PROXY_UPSTREAM  the daemon's socket (default /var/run/docker.sock)
  BLPL_PROXY_*         the policy; see ProxyPolicy.from_env
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import h11
import httpx

from .policy import (
    ProxyPolicy,
    Refused,
    check_create,
    check_exec_create,
    check_exec_start,
    check_labels,
    check_query,
    check_volume_create,
    check_volume_labels,
    force_list_filters,
    parse_body,
    route,
)

logger = logging.getLogger("blpl.sandbox.proxy")

MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 1024 * 1024
# How long a client may take to send one request. h11 already refuses a
# malformed or negative Content-Length and the body is capped as it arrives;
# this bounds a request that trickles in, or never ends.
REQUEST_TIMEOUT_S = 30.0
_FULL_ID = re.compile(r"[0-9a-f]{64}")


class Missing(Refused):
    """The thing named does not exist. Answered 404, which callers rely on."""


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    body: bytes = b""


@dataclass(frozen=True)
class Forward:
    method: str
    path: str
    query: dict[str, str]
    body: bytes | None = None

    def encode(self) -> bytes:
        target = self.path + ("?" + urlencode(self.query) if self.query else "")
        lines = [
            f"{self.method} {target} HTTP/1.1",
            "Host: docker",
            "User-Agent: blpl-docker-proxy",
            "Connection: close",
        ]
        if self.body is not None:
            lines += ["Content-Type: application/json", f"Content-Length: {len(self.body)}"]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + (self.body or b"")


class Upstream(Protocol):
    async def get_json(self, path: str) -> tuple[int, Any]: ...
    async def open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...


class UnixUpstream:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://docker", timeout=30.0,
        )

    async def get_json(self, path: str) -> tuple[int, Any]:
        r = await self._http.get(path)
        try:
            return r.status_code, r.json() if r.content else None
        except ValueError:
            return r.status_code, None

    async def open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_unix_connection(self.socket_path)

    async def aclose(self) -> None:
        await self._http.aclose()


def _single_query(raw: str) -> dict[str, str]:
    parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=False)
    out = {}
    for k, vals in parsed.items():
        if len(vals) != 1:
            raise Refused(f"query parameter {k!r} given more than once")
        out[k] = vals[0]
    return out


class DockerProxy:
    def __init__(self, policy: ProxyPolicy, upstream: Upstream):
        self.policy = policy
        self.upstream = upstream

    async def _container(self, v: str, ident: str) -> str:
        """Resolve a container reference to its full id, if it is ours."""
        status, info = await self.upstream.get_json(f"/{v}/containers/{ident}/json")
        if status == 404:
            raise Missing(f"No such container: {ident}")
        if status != 200 or not isinstance(info, dict):
            raise Refused(f"could not inspect {ident} ({status})")
        check_labels(self.policy, (info.get("Config") or {}).get("Labels"), f"container {ident}")
        cid = info.get("Id", "")
        if not _FULL_ID.fullmatch(cid):
            raise Refused(f"container {ident} has no usable id")
        return cid

    async def decide(self, req: Request) -> Forward:
        r = route(self.policy, req.method, req.path)
        q = check_query(r.action, req.query)
        v = r.version
        p = self.policy

        if r.action in ("ping", "version"):
            return Forward(req.method, f"/{v}/{'_ping' if r.action == 'ping' else 'version'}", {})

        if r.action == "list":
            q["filters"] = force_list_filters(p, q.get("filters"))
            return Forward("GET", f"/{v}/containers/json", q)

        if r.action == "create":
            plan = check_create(p, q, parse_body(req.body))
            for vol in plan.volumes:
                status, info = await self.upstream.get_json(f"/{v}/volumes/{vol}")
                if status != 200 or not isinstance(info, dict):
                    # A mount naming a missing volume would make the daemon
                    # create one, unlabelled. Volumes are created on purpose.
                    raise Refused(f"volume {vol} does not exist")
                check_volume_labels(p, info.get("Labels"), plan.project, vol)
            return Forward("POST", f"/{v}/containers/create", {"name": plan.name},
                           json.dumps(plan.body).encode())

        if r.action == "volume_create":
            body = check_volume_create(p, parse_body(req.body))
            return Forward("POST", f"/{v}/volumes/create", {}, json.dumps(body).encode())

        if r.action == "volume_inspect":
            assert r.ident is not None
            if not r.ident.startswith(p.volume_prefix):
                raise Refused(f"volume {r.ident} is not permitted")
            status, info = await self.upstream.get_json(f"/{v}/volumes/{r.ident}")
            if status == 404:
                raise Missing(f"No such volume: {r.ident}")
            labels = (info or {}).get("Labels") if isinstance(info, dict) else None
            if not isinstance(labels, dict) or labels.get("blpl.managed") != "true" \
                    or labels.get("blpl.instance") != p.instance:
                raise Refused(f"volume {r.ident} is not managed by this deployment")
            return Forward("GET", f"/{v}/volumes/{r.ident}", {})

        if r.action in ("exec_start", "exec_inspect"):
            assert r.ident is not None
            status, info = await self.upstream.get_json(f"/{v}/exec/{r.ident}/json")
            if status == 404:
                raise Missing(f"No such exec instance: {r.ident}")
            if status != 200 or not isinstance(info, dict):
                raise Refused(f"could not inspect exec {r.ident} ({status})")
            await self._container(v, str(info.get("ContainerID", "")))
            eid = str(info.get("ID", ""))
            if not _FULL_ID.fullmatch(eid):
                raise Refused("exec instance has no usable id")
            if r.action == "exec_inspect":
                return Forward("GET", f"/{v}/exec/{eid}/json", {})
            body = check_exec_start(parse_body(req.body))
            return Forward("POST", f"/{v}/exec/{eid}/start", {}, json.dumps(body).encode())

        # Everything left names a container in the path.
        assert r.ident is not None
        cid = await self._container(v, r.ident)
        if r.action == "inspect":
            return Forward("GET", f"/{v}/containers/{cid}/json", {})
        if r.action == "logs":
            # A run container's output (phase 4). Read-only, and only ours.
            return Forward("GET", f"/{v}/containers/{cid}/logs", q)
        if r.action == "op":
            return Forward("POST", f"/{v}/containers/{cid}/{r.op}", q)
        if r.action == "remove":
            return Forward("DELETE", f"/{v}/containers/{cid}", q)
        if r.action == "exec_create":
            body = check_exec_create(parse_body(req.body))
            return Forward("POST", f"/{v}/containers/{cid}/exec", {}, json.dumps(body).encode())
        raise Refused(f"unhandled action {r.action}")  # pragma: no cover

    # -- connection handling ------------------------------------------------------

    async def _read_request(self, conn: h11.Connection, reader: asyncio.StreamReader) -> Request | None:
        method = target = None
        body = bytearray()
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                data = await asyncio.wait_for(reader.read(65536), REQUEST_TIMEOUT_S)
                conn.receive_data(data)
                if not data and conn.their_state is not h11.DONE:
                    return None
                continue
            if isinstance(event, h11.Request):
                method, target = event.method.decode("ascii"), event.target.decode("ascii")
                # Refused on what it declares, before any of it is read.
                for name, value in event.headers:
                    if name == b"content-length" and int(value) > MAX_BODY_BYTES:
                        raise Refused("request body too large")
            elif isinstance(event, h11.Data):
                body += event.data
                if len(body) > MAX_BODY_BYTES:
                    raise Refused("request body too large")
            elif isinstance(event, h11.EndOfMessage):
                break
            elif isinstance(event, h11.ConnectionClosed):
                return None
        assert method is not None and target is not None
        parts = urlsplit(target)
        return Request(method, parts.path, _single_query(parts.query), bytes(body))

    async def _reply(self, conn: h11.Connection, writer: asyncio.StreamWriter,
                     status: int, message: str) -> None:
        body = json.dumps({"message": message}).encode()
        out = conn.send(h11.Response(status_code=status, headers=[
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Connection", "close"),
        ]))
        out += conn.send(h11.Data(data=body)) + conn.send(h11.EndOfMessage())
        writer.write(out)
        await writer.drain()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEADER_BYTES)
        up_writer = None
        try:
            try:
                req = await self._read_request(conn, reader)
            except h11.RemoteProtocolError as e:
                await self._reply(conn, writer, 400, f"bad request: {e}")
                return
            except Refused as e:
                await self._reply(conn, writer, 413, str(e))
                return
            except asyncio.TimeoutError:
                await self._reply(conn, writer, 408, "the request did not arrive in time")
                return
            if req is None:
                return
            try:
                fwd = await self.decide(req)
            except Missing as e:
                logger.info("missing %s %s: %s", req.method, req.path, e)
                await self._reply(conn, writer, 404, str(e))
                return
            except Refused as e:
                logger.warning("refused %s %s: %s", req.method, req.path, e)
                await self._reply(conn, writer, 403, f"blpl-docker-proxy: {e}")
                return
            logger.info("forward %s %s", fwd.method, fwd.path)
            up_reader, up_writer = await self.upstream.open()
            up_writer.write(fwd.encode())
            await up_writer.drain()
            while chunk := await up_reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # either side went away; nothing to tell anyone
        finally:
            if up_writer is not None:
                up_writer.close()
            writer.close()


async def serve(proxy: DockerProxy, socket_path: str) -> asyncio.AbstractServer:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_socket():
        path.unlink()
    server = await asyncio.start_unix_server(proxy.handle, path=str(path))
    # Group-accessible so the control plane's group can connect; nobody else.
    os.chmod(path, 0o660)
    return server


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
    policy = ProxyPolicy.from_env()
    if not policy.images:
        logger.error("BLPL_PROXY_IMAGES is empty: nothing could ever be created; refusing to start")
        sys.exit(1)
    upstream = UnixUpstream(os.environ.get("BLPL_PROXY_UPSTREAM", "/var/run/docker.sock"))
    listen = os.environ.get("BLPL_PROXY_LISTEN", "/run/blpl-docker-proxy/docker.sock")
    server = await serve(DockerProxy(policy, upstream), listen)
    logger.info("listening on %s for instance %r; images %s", listen, policy.instance,
                sorted(policy.images))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
