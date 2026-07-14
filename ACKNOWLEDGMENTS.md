# Acknowledgments

BLPL stands on other people's work. Everything below is used under a permissive
license, and none of it is modified in place — we vendor what we need into our
own tree and build on top, leaving the upstream projects untouched.

## KiCAD-Prism — Apache License 2.0

<https://github.com/krishna-swaroop/KiCAD-Prism> — Krishna Swaroop and contributors.

Prism is a web platform for browsing, reviewing, and operating on KiCad
repositories from the browser. Two ideas of theirs are load-bearing here:

- **KiCad as the base image.** Building the backend `FROM kicad/kicad`, so
  `kicad-cli` is part of the deployment rather than a per-workstation install.
  This is the single decision that makes moving between machines painless, and
  we took it directly from `backend/Dockerfile`.
- **The ecad-viewer integration.** `app/frontend/src/components/Visualizer.tsx`
  is adapted from Prism's `frontend/src/components/visualizer.tsx` — the
  `<ecad-blob>` hydration sequence and `load_src()` handshake are theirs. The
  TypeScript declarations in `app/frontend/src/types/ecad-viewer.d.ts` are
  copied from Prism.

Changes we made, as Apache-2.0 asks us to state: the viewer host was rewritten
to fetch from BLPL's own `/api/projects/{id}/design` endpoint and to reload when
a pipeline stage emits a new board; comment overlays and cross-probing are not
yet wired up. We did **not** adopt Prism's job runner — BLPL streams stage output
over SSE rather than polling a job table.

## ecad-viewer — MIT

<https://github.com/Huaqiu-Electronics/ecad-viewer> — Huaqiu Electronics.

The browser-side KiCad renderer. `.kicad_sch` and `.kicad_pcb` files are parsed
and drawn client-side. Vendored as prebuilt bundles in `app/frontend/public/`
(`ecad-viewer.js`, `glyph-full.js`, `3d-viewer.js`, `three/`).

ecad-viewer itself builds on:

- **KiCanvas** — MIT — <https://github.com/theacodes/kicanvas> — Thea Flowers.
  The original in-browser KiCad renderer that ecad-viewer derives from.
- **three-gltf-viewer** — MIT — <https://github.com/donmccurdy/three-gltf-viewer> —
  Don McCurdy. Backs the 3D view.

## kicad-happy — MIT

<https://github.com/aklofas/kicad-happy> — Andrew Klofas.

The design-review analyzers behind `blpl stage8`: schematic and PCB parsing,
subcircuit detection, EMC pre-compliance, SPICE, thermal. See
`kicad-happy/ATTRIBUTION.md` for the details of our divergence.

## KiCad — GPL / CC-BY-SA

<https://www.kicad.org> — the KiCad project and its contributors.

The official `kicad/kicad:10.0.0` container image provides `kicad-cli`. The
symbol, footprint, and 3D model libraries (`kicad-symbols`, `kicad-footprints`,
`kicad-packages3D`) are used as published. KiCad is invoked as a separate
program over its CLI; it is not linked into BLPL.
