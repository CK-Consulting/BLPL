# The stock linuxserver KiCad image, plus the Python that PCM cannot install.
#
# KiCad's Plugin and Content Manager distributes GUI plugins only. KiKit is
# three pieces — a backend Python library, a PCM GUI plugin, and PCM
# libraries — and PCM can deliver only the last two, so a PCM-installed KiKit
# greets its first use with "No KiKit backend found". The backend has to be
# pip-installed into the SAME interpreter KiCad's plugins run in, which on
# this Alpine-based image is the system python3 (the one that can already
# `import pcbnew`).
#
# It is a layer on the published image, not a fork of its Dockerfile: nothing
# about how upstream builds KiCad is ours to maintain, and `latest` keeps
# tracking their releases. psutil rides along for CopperFiller, which checks
# for shapely (KiKit already requires it) and psutil at plugin load.
#
# PCM plugins themselves live under /config — the persistent volume — so they
# survive container recreation on their own. What does NOT survive recreation
# is anything pip-installed into the running container, which is exactly why
# this file exists.
FROM lscr.io/linuxserver/kicad:latest

RUN pip3 install --no-cache-dir --break-system-packages \
    kikit \
    psutil
