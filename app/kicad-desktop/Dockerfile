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
# about how upstream builds KiCad is ours to maintain. psutil rides along for
# CopperFiller, which checks for shapely (KiKit already requires it) and psutil
# at plugin load.
#
# **Pinned, because it used to be `latest`.** The pipeline runs a pinned
# kicad/kicad:10.0.0 and this desktop floated; at one point the desktop was
# 10.0.3, the pipeline 10.0.0 and the host kicad-cli 10.0.6 — three KiCads in
# one workflow, silently. That matters because a symbol or footprint drawn here
# is read by the pipeline, and file-format drift between KiCad versions is a
# real failure this project has already hit (a batch of 2021-format symbols
# loaded with no pins and produced 37 ERC violations on one board).
#
# The versions still are not equal: linuxserver's newest KiCad is 10.0.3 and
# there is no 10.0.0 tag, so they cannot be matched exactly from here. Pinning
# at least makes the skew fixed and visible instead of moving. Closing it means
# moving the pipeline to 10.0.3, which changes what emits every board and is a
# decision on its own.
#
# PCM plugins themselves live under /config — the persistent volume — so they
# survive container recreation on their own. What does NOT survive recreation
# is anything pip-installed into the running container, which is exactly why
# this file exists.
FROM lscr.io/linuxserver/kicad:10.0.3-r0-ls150

RUN pip3 install --no-cache-dir --break-system-packages \
    kikit \
    psutil
