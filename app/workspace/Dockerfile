# The workspace image: the headless role of the blpl-kicad image family.
#
# A workspace is the per-project container the pipeline, the agent's file tools
# and (later) the terminal run in — see docs/architecture-v2.md. It is the
# backend image minus everything that makes the backend the control plane: no
# FastAPI, no alembic, no app/ code, no database driver, no entrypoint that
# migrates anything. What is left is KiCad and kicad-cli, Freerouting, pdftotext,
# git and the blpl pipeline, at exactly the versions the backend uses today.
#
# Two stages, and the first is the point of the file:
#
#   kicad-base   KiCad, kicad-cli, Java + Freerouting, Python venv, the pipeline.
#                The future desktop role builds FROM this same stage and adds
#                streaming and a window manager, so the KiCad a member opens is
#                the KiCad the pipeline plotted with.
#   workspace    non-root, read-only-rootfs friendly, nothing baked in that is
#                secret, and a command that just keeps the container alive.
#
# Until the backend stops running stages (phase 3), the backend Dockerfile keeps
# its own copy of the base instructions. test_workspace_image.py asserts the two
# copies are identical, so they share Docker's layer cache and cannot drift to
# different KiCad or Freerouting versions without a test failing.
#
# Build from the umbrella checkout's root (it needs core/ and plugin/):
#   docker build -f app/workspace/Dockerfile -t blpl-workspace:dev .

# ---- kicad-base: identical to the top of app/backend/Dockerfile --------------
FROM kicad/kicad:10.0.0 AS kicad-base
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip python3-venv git poppler-utils ca-certificates curl gnupg \
    && rm -rf /var/lib/apt/lists/*
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
ENV VIRTUAL_ENV=/opt/blpl-venv
RUN python3 -m venv --system-site-packages $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# The pipeline and its analyzers, at the paths the backend image uses: the
# package's parents[2] is /app, which is where the CLI looks for kicad-happy and
# for the stock libraries (bind-mounted read-only at /app/kicad-symbols and
# /app/kicad-footprints by the driver, never copied in).
COPY core/pyproject.toml /app/
COPY core/blpl /app/blpl
COPY core/schemas /app/schemas
COPY core/kicad-happy /app/kicad-happy
RUN pip install --no-cache-dir -e /app[all-llm]
COPY plugin/blpl_kicad_plugin /app/plugin/blpl_kicad_plugin

ENV BLPL_KICAD_HAPPY=/app/kicad-happy \
    BLPL_KICAD_PLUGIN_DIR=/app/plugin \
    FREEROUTING_JAR=/opt/freerouting/freerouting.jar \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---- workspace: the headless role -------------------------------------------
FROM kicad-base AS workspace

# Same uid handling as the backend, for the same reasons: files the workspace
# writes into project storage must be owned by the host user that owns the rest
# of it, and ssh (git over ssh remotes) needs a passwd entry for the uid. The
# driver runs the container with this numeric uid explicitly; the socket proxy
# refuses uid 0 whatever the image says.
ARG BLPL_UID=1000
ARG BLPL_GID=1000
RUN set -eux; \
    getent group "${BLPL_GID}" >/dev/null || groupadd -g "${BLPL_GID}" blpl; \
    if getent passwd "${BLPL_UID}" >/dev/null; then \
        usermod -d /home/blpl "$(getent passwd "${BLPL_UID}" | cut -d: -f1)"; \
    else \
        useradd -u "${BLPL_UID}" -g "${BLPL_GID}" -d /home/blpl -s /bin/sh blpl; \
    fi; \
    mkdir -p /home/blpl /workspace; \
    chown "${BLPL_UID}:${BLPL_GID}" /home/blpl /workspace

# /workspace is where the project's volume mounts. Docker seeds an empty named
# volume from the image directory it covers, ownership included, so a new
# project's storage starts out owned by BLPL_UID rather than root.
WORKDIR /workspace

# The driver mounts the root filesystem read-only and /tmp as tmpfs, so HOME
# points into /tmp: KiCad writes its config there on first start.
ENV HOME=/tmp

USER ${BLPL_UID}:${BLPL_GID}

LABEL org.opencontainers.image.title="blpl-workspace" \
      org.opencontainers.image.description="BLPL per-project workspace (headless KiCad + pipeline)"

# The workspace agent (phase 3): the control plane's one way in. Two files of
# the backend, both stdlib only. agent.py is the server. projects.py is the
# backend's git layer, so status, commit, pull and push behave exactly as they
# do there. Nothing else of the control plane is in this image. Root-owned and
# on the read-only rootfs, so a run cannot replace either file.
COPY app/backend/app/sandbox/agent.py /opt/blpl/wsagent/agent.py
COPY app/backend/app/projects.py /opt/blpl/wsagent/projects.py

# The agent serves when the control plane gave it a directory and a token
# (/run/blpl). Without them (a phase 2 workspace) it only holds the container
# open. The driver sets Init so a real PID 1 reaps what kicad-cli and java leave.
CMD ["python", "/opt/blpl/wsagent/agent.py"]
