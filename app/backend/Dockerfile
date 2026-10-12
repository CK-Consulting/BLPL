# The backend image IS a KiCad image. That is the load-bearing decision here:
# kicad-cli ships in the container, so no workstation ever needs KiCad installed
# or updated, and every machine you sit at renders and exports identically.
#
# Official upstream image, deliberately. Huaqiu-Electronics publish a kicad:full
# with 3D models baked in, but it is built from a fork whose default branch is
# release/9.0 with no documented divergence from upstream — and we emit KiCad 10
# files. We take the official 10.0.0 image and mount the 3D model libraries we
# already vendor instead, which gets `pcb export glb --subst-models` working
# without inheriting an unaudited fork.
FROM kicad/kicad:10.0.0

USER root

# poppler-utils supplies pdftotext, which is how a datasheet is searched for
# the part it covers. Not optional in practice: PDFs subset their fonts, so a
# raw byte scan reads "ORDERING" out of an ordering table and misses the part
# being ordered. kicad-happy's datasheet verification already reaches for the
# same binary and silently degrades without it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip python3-venv git poppler-utils ca-certificates curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# Java and Freerouting, so `blpl run` can autoroute after stage6. Without a
# router every net on every emitted board is unrouted by construction, and
# Stage 8 can only file RT-001 under "known limitation"; once a router has had
# a go, an unrouted net is a finding about the design. Freerouting 2.4.x needs
# Java 25, which Debian trixie's own openjdk (21) does not satisfy, so the JRE
# comes from Adoptium's apt repository. The jar is pinned by version and by
# sha256: a jar that changed under us would change what routes, silently.
ARG FREEROUTING_VERSION=2.4.1
ARG FREEROUTING_SHA256=251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9
RUN set -eux; \
    curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public \
        | gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb trixie main" \
        > /etc/apt/sources.list.d/adoptium.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends temurin-25-jre; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /opt/freerouting; \
    curl -fsSL -o /opt/freerouting/freerouting.jar \
        "https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/freerouting-${FREEROUTING_VERSION}.jar"; \
    echo "${FREEROUTING_SHA256}  /opt/freerouting/freerouting.jar" | sha256sum -c -

WORKDIR /app

# A venv, not --break-system-packages: the KiCad image's system Python owns
# pcbnew, and we would rather not fight it over dependency versions.
ENV VIRTUAL_ENV=/opt/blpl-venv
RUN python3 -m venv --system-site-packages $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY app/backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# BLPL itself (stages 0-8) plus the review analyzers. The pipeline is its own
# tree, core/ (the future "blpl-core" repository), so the build context is the
# umbrella checkout's root, one level above app/, to reach it. It lands at the
# same /app paths it always has: the package's parents[2] is /app, which is where
# the CLI looks for kicad-happy and the stock libraries.
#
# The kicad-symbols/ and kicad-footprints/ libraries are deliberately NOT copied
# here — they are large submodules and are bind-mounted at runtime into
# /app/kicad-symbols and /app/kicad-footprints (the CLI's default library roots),
# alongside the 3D models. See app/docker-compose.yml. The pipeline cannot resolve
# stock symbols/footprints without those mounts.
COPY core/pyproject.toml /app/
COPY core/blpl /app/blpl
COPY core/schemas /app/schemas
COPY core/kicad-happy /app/kicad-happy
# [all-llm] pulls in the anthropic/openai/ollama SDKs. The webapp lets a project
# configure a fallback chain over any of the three, and each provider backend
# lazy-imports its SDK — so without the extra, Stage 0/1 die with
# ModuleNotFoundError the moment that provider is used (anthropic is the default).
RUN pip install --no-cache-dir -e /app[all-llm]

# The KiCad PCB plugin (plugin/, the future "blpl-kicad-plugin"), for the
# stage6-plugin stage. It runs under KiCad's own Python, not this venv, so it is
# not pip-installed: the stage puts the directory holding the package on that
# interpreter's PYTHONPATH, and BLPL_KICAD_PLUGIN_DIR (below) says where that is.
# It used to arrive inside blpl/ as blpl.plugin_kicad.
COPY plugin/blpl_kicad_plugin /app/plugin/blpl_kicad_plugin

COPY app/backend/app /app/app
COPY app/backend/alembic.ini /app/alembic.ini
COPY app/backend/alembic /app/alembic
COPY app/backend/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV BLPL_PROJECTS_ROOT=/app/projects \
    BLPL_KICAD_HAPPY=/app/kicad-happy \
    BLPL_KICAD_PLUGIN_DIR=/app/plugin \
    FREEROUTING_JAR=/opt/freerouting/freerouting.jar \
    PYTHONUNBUFFERED=1

# Run as the host user who owns app/data, not as root.
#
# Everything these containers write lands in the app/data bind mount, and as
# root it landed root:root: host-side git and edits needed sudo, and a chown
# back to the host user drifted again with the next file a run wrote. Worse,
# git refuses a repository owned by someone other than the caller, so after
# such a chown a root worker could no longer commit to the projects at all.
# Matching the uid removes both.
#
# /run/blpl-docker-proxy is where the socket proxy (phase 2 of
# docs/architecture-v2.md, the same image with another entrypoint) listens, in
# a named volume the worker also mounts. Docker seeds an empty named volume
# from the image's directory, ownership included, so owning it here is what
# lets a non-root proxy create its socket there.
#
# The uid needs a passwd entry, not just a number in compose's `user:`: ssh
# (SSH git remotes) exits with "No user exists for uid" without one, and finds
# ~/.ssh through the entry's home directory rather than $HOME. The KiCad image
# may already own that uid (its own `kicad` user is 1000), so an existing
# entry is re-homed rather than duplicated. Compose passes the same numbers as
# build args and as `user:`; entrypoint.sh refuses to start if they disagree.
ARG BLPL_UID=1000
ARG BLPL_GID=1000
RUN set -eux; \
    getent group "${BLPL_GID}" >/dev/null || groupadd -g "${BLPL_GID}" blpl; \
    if getent passwd "${BLPL_UID}" >/dev/null; then \
        usermod -d /home/blpl "$(getent passwd "${BLPL_UID}" | cut -d: -f1)"; \
    else \
        useradd -u "${BLPL_UID}" -g "${BLPL_GID}" -d /home/blpl -s /bin/sh blpl; \
    fi; \
    mkdir -p /home/blpl/.ssh; \
    chown -R "${BLPL_UID}:${BLPL_GID}" /home/blpl; \
    mkdir -p /run/blpl-docker-proxy; \
    chown "${BLPL_UID}:${BLPL_GID}" /run/blpl-docker-proxy
ENV HOME=/home/blpl \
    PYTHONDONTWRITEBYTECODE=1
USER ${BLPL_UID}:${BLPL_GID}

EXPOSE 8000

# Migrations run before the server does — see entrypoint.sh for why that is a
# script and not a compose `command`.
ENTRYPOINT ["/app/entrypoint.sh"]

# One worker on purpose. Stage runs stream over SSE from an in-process
# subprocess; adding workers without a shared job store just makes streams land
# on the wrong process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
