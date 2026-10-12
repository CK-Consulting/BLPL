"""Per-project workspace containers, and the narrow door the control plane
reaches them through.

The design is docs/architecture-v2.md. In one paragraph: the backend holds every
secret the deployment has, so it must not also be the process that runs
kicad-cli, pdftotext and Freerouting over files that arrived from outside. Each
project gets a container of its own — its storage, the shared libraries
read-only, no service secrets, egress only through an allowlisting proxy — and
the backend asks for things to happen in it through a driver.

What lives here:

* ``driver``   the interface and its value types. Everything else depends on it,
               and it depends on nothing, so a Nomad or Kubernetes driver later
               is a new module, not an edit.
* ``fake``     ``FakeDriver``, an in-memory driver for tests.
* ``docker``   ``DockerDriver``, the Engine API over a configurable endpoint.
               In a deployment that endpoint is the socket proxy, never the
               daemon's own socket.
* ``policy``   what the socket proxy permits, as pure functions.
* ``proxy``    the socket proxy itself: a separate process that alone holds the
               real Docker socket.
* ``idle``     activity tracking and the idle-stop sweep.
* ``storage``  where a project's files live, behind an interface that a quota-
               enforcing implementation can replace.
* ``runpath``  the seam the run path will call: one function that takes a run
               through a driver with an environment built from an allowlist.

Nothing in the app imports this package yet. That is deliberate (phase 1 of the
migration in the architecture document): it lands, is tested against a real
daemon, and is wired into the worker as a separate change.

This package must stay importable without FastAPI or SQLAlchemy. The socket
proxy runs in a container that has neither, and the tests run on a bare host.
"""
