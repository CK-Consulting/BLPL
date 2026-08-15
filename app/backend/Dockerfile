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

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip python3-venv git \
    && rm -rf /var/lib/apt/lists/*

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
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Migrations run before the server does — see entrypoint.sh for why that is a
# script and not a compose `command`.
ENTRYPOINT ["/app/entrypoint.sh"]

# One worker on purpose. Stage runs stream over SSE from an in-process
# subprocess; adding workers without a shared job store just makes streams land
# on the wrong process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
