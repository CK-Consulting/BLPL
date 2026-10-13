"""Where documents come from: the network directly, or a service that fetches
them on this process's behalf.

Standalone (the CLI, the KiCad plugin, a host run), nothing changes: the
distributor scripts in kicad-happy resolve a part and download its datasheet
themselves, as they always have, and the file goes through ``quarantine``
here. ``get_fetcher()`` returns None for that.

Inside a BLPL workspace there is no general internet: the egress proxy
allows LLM, distributor API and git hosts only. Documents are fetched by the
control plane instead (app/backend/app/fetchsvc.py), which refuses private
and link-local addresses on every hop, caps the size, checks the bytes are a
PDF, and puts them through its own quarantine and scanner. Only a file it
accepts comes back. A file it holds stays with it, and this side records the
verdict in the project's ledger without the bytes. The service is named by
``BLPL_FETCH_SERVICE`` and authenticated with the run's own credential,
``BLPL_FETCH_AUTH``; both are given to one run and nothing else.

Fetcher is the seam: anything with ``get(url)`` returning a ``Fetched``.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import quarantine


@dataclass
class Fetched:
    """What came back: the bytes, or the reason there are none."""

    url: str
    data: bytes | None = None
    held: bool = False
    verdict: dict = field(default_factory=dict)
    error: str = ""


class Fetcher(Protocol):
    def get(self, url: str) -> Fetched: ...


class ServiceFetcher:
    """The control plane's fetch service (app/backend/app/fetchsvc.py)."""

    def __init__(self, service: str, auth: str, *, timeout: float = 180.0):
        self.service = service.rstrip("/")
        self.auth = auth
        self.timeout = timeout

    def get(self, url: str) -> Fetched:
        body = json.dumps({"url": url}).encode()
        req = urllib.request.Request(
            f"{self.service}/fetch", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Basic " + base64.b64encode(self.auth.encode()).decode()})
        # Straight to the service on the workspace's own network: never
        # through the egress proxy, which would refuse it anyway.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return Fetched(url=url, data=resp.read())
        except urllib.error.HTTPError as exc:
            try:
                answer = json.loads(exc.read() or b"{}")
            except ValueError:
                answer = {}
            if exc.code == 422 and answer.get("held"):
                return Fetched(url=url, held=True, verdict=answer)
            return Fetched(url=url, error=str(answer.get("error") or f"HTTP {exc.code}"))
        except (OSError, ValueError) as exc:
            return Fetched(url=url, error=f"the fetch service could not be reached: {exc}")


def get_fetcher(environ=os.environ) -> Fetcher | None:
    """The configured fetcher, or None to fetch directly as always."""
    service = environ.get("BLPL_FETCH_SERVICE", "").strip()
    if not service:
        return None
    return ServiceFetcher(service, environ.get("BLPL_FETCH_AUTH", ""))


def retrieve_into(
    fetcher: Fetcher,
    url: str,
    project_dir: str | Path,
    *,
    mpn: str = "",
    distributor: str = "",
    as_name: str = "",
    dest_dirname: str = quarantine.TRUSTED_DIRNAME,
) -> dict | None:
    """Fetch url and file it in the project the way a direct download is.

    Accepted bytes go through this project's own quarantine as well, so the
    ledger and the release are exactly what a direct download produces. A
    file the fetcher held is recorded as held, with no bytes. None when
    nothing was fetched at all."""
    got = fetcher.get(url)
    if got.held:
        v = got.verdict
        return quarantine.note_held_elsewhere(
            project_dir, sha256=str(v.get("sha256") or ""), size=int(v.get("bytes") or 0),
            reasons=[str(r) for r in v.get("reasons") or []],
            inspection=v.get("inspection") or {}, scan=v.get("scan") or {},
            mpn=mpn, distributor=distributor, source_url=url,
        ).to_dict()
    if not got.data:
        return None
    return quarantine.accept_and_release(
        got.data, project_dir, mpn=mpn, distributor=distributor, source_url=url,
        as_name=as_name, dest_dirname=dest_dirname,
    ).to_dict()
