"""KiCad v9/v10 emitters for schematic, PCB, and project files.

Targets the KiCad 10.x S-expression formats. Replaces the pre-v7 generators in
the legacy ``yaml_to_kicad.py`` at the pipeline root, which emit a schema that
``kicad-cli`` refuses to load.

Design points:
  - Footprints are *reference-by-name*: the PCB includes a full footprint block
    per component, but the pad geometry is copied from the referenced library
    footprint in kicad-footprints/ rather than re-invented here. That keeps the
    PCB small and editable: refreshing footprints in KiCad picks up library
    changes automatically.
  - Schematic lib_symbols entries are empty-shell declarations. KiCad resolves
    the actual symbol graphics from the project's symbol-library table at
    open-time. This matches what KiCad itself emits for designs whose symbols
    live in installed libraries.
"""

# Submodules (sexpr, loaders, pcb, sch, pro) are imported lazily by callers.
