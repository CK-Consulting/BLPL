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

# BLPL itself (stages 0-8) plus the review analyzers. Build context is the
# board-layer-pipe-line/ root so both are reachable.
#
# The kicad-symbols/ and kicad-footprints/ libraries are deliberately NOT copied
# here — they are large submodules and are bind-mounted at runtime into
# /app/kicad-symbols and /app/kicad-footprints (the CLI's default library roots),
# alongside the 3D models. See app/docker-compose.yml. The pipeline cannot resolve
# stock symbols/footprints without those mounts.
COPY pyproject.toml README.md /app/
COPY blpl /app/blpl
COPY schemas /app/schemas
COPY kicad-happy /app/kicad-happy
# [all-llm] pulls in the anthropic/openai/ollama SDKs. The webapp lets a project
# configure a fallback chain over any of the three, and each provider backend
# lazy-imports its SDK — so without the extra, Stage 0/1 die with
# ModuleNotFoundError the moment that provider is used (anthropic is the default).
RUN pip install --no-cache-dir -e /app[all-llm]

COPY app/backend/app /app/app
COPY app/backend/alembic.ini /app/alembic.ini
COPY app/backend/alembic /app/alembic
COPY app/backend/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV BLPL_PROJECTS_ROOT=/app/projects \
    BLPL_KICAD_HAPPY=/app/kicad-happy \
    FREEROUTING_JAR=/opt/freerouting/freerouting.jar \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Migrations run before the server does — see entrypoint.sh for why that is a
# script and not a compose `command`.
ENTRYPOINT ["/app/entrypoint.sh"]

# One worker on purpose. Stage runs stream over SSE from an in-process
# subprocess; adding workers without a shared job store just makes streams land
# on the wrong process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
