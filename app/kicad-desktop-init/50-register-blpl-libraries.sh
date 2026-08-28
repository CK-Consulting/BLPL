#!/usr/bin/env bash
# Registers BLPL's libraries in the KiCad desktop's global library tables, so
# that the desktop and the pipeline finally agree about where symbols live.
#
# Without this, the desktop offers exactly the wrong save targets. Every
# library KiCad lists is either the stock mount or a PCM install: stock is
# read-only on purpose (see the compose file — membership of one board must
# not become write access to the library behind all of them), and a PCM
# library lives inside /config where only the desktop can see it. So "draw
# the missing symbol" — the single task this desktop exists for — either
# fails with a read-only error or succeeds into a file the pipeline never
# searches. The first symbol anyone drew here landed in a third-party
# Elektuur library, invisible to every stage.
#
# What the pipeline searches, highest priority first, is each project's own
# libraries/ (the place manual_library_work.md tells people to save into),
# then the shared vendor modules, then generated, then stock. This script
# makes the desktop present that same list: one writable library per project,
# named after the project, backed by libraries/symbols/<name>.kicad_sym and
# libraries/footprints/<name>.pretty — and the shared vendor libraries
# registered read-only under their own names, so a `Infineon:...` reference
# reads the same in the schematic editor as in bom.json.
#
# Runs at container start (linuxserver /custom-cont-init.d), as root, before
# the desktop session. Idempotent: an already-registered nickname is left
# alone, so a user's own edits to the tables survive restarts. KiCad only
# reads these tables when the application starts, which is exactly when this
# runs.

set -u

KICAD_CONFIG_DIR="/config/.config/kicad"
PROJECTS_DIR="/config/projects"
SHARED_DIR="/config/shared-libraries"

# The newest kicad config dir present, or 10.0 on a first boot where the
# desktop has not created one yet — the table files are created either way.
ver="$(ls -1 "$KICAD_CONFIG_DIR" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+$' | sort -V | tail -1)"
ver="${ver:-10.0}"
mkdir -p "$KICAD_CONFIG_DIR/$ver"
SYM_TABLE="$KICAD_CONFIG_DIR/$ver/sym-lib-table"
FP_TABLE="$KICAD_CONFIG_DIR/$ver/fp-lib-table"

[ -s "$SYM_TABLE" ] || printf '(sym_lib_table\n\t(version 7)\n)\n' > "$SYM_TABLE"
[ -s "$FP_TABLE" ] || printf '(fp_lib_table\n\t(version 7)\n)\n' > "$FP_TABLE"

# add_entry <table-file> <nickname> <uri> <descr>
# Appends a (lib ...) row before the table's closing paren, unless the
# nickname is already registered.
add_entry() {
    local table="$1" nick="$2" uri="$3" descr="$4"
    grep -qF "(name \"$nick\")" "$table" && return 0
    # Drop the final line holding the lone closing paren, append row + paren.
    local tmp="$table.blpl-tmp"
    sed '$d' "$table" > "$tmp"
    printf '\t(lib (name "%s") (type "KiCad") (uri "%s") (options "") (descr "%s"))\n)\n' \
        "$nick" "$uri" "$descr" >> "$tmp"
    mv "$tmp" "$table"
    echo "kicad-init: registered $nick -> $uri"
}

# --- one writable library per project ---------------------------------------
for proj in "$PROJECTS_DIR"/*/; do
    [ -d "$proj" ] || continue
    proj="${proj%/}"
    name="$(basename "$proj")"
    symdir="$proj/libraries/symbols"
    fpdir="$proj/libraries/footprints"
    # Only projects that have the libraries/ layout (blpl init creates it).
    [ -d "$proj/libraries" ] || continue
    mkdir -p "$symdir" "$fpdir"
    symfile="$symdir/$name.kicad_sym"
    if [ ! -s "$symfile" ]; then
        # A minimal valid, empty library: a file KiCad will open and save
        # into, and the flat layout every pipeline stage already indexes.
        printf '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "blpl")\n\t(generator_version "1.0")\n)\n' > "$symfile"
    fi
    mkdir -p "$fpdir/$name.pretty"
    add_entry "$SYM_TABLE" "$name" "$symfile" "BLPL project library (writable) — searched FIRST by the pipeline"
    add_entry "$FP_TABLE" "$name" "$fpdir/$name.pretty" "BLPL project library (writable) — searched FIRST by the pipeline"
done

# --- the shared vendor libraries, read-only, under their pipeline names -----
if [ -d "$SHARED_DIR" ]; then
    find "$SHARED_DIR" -maxdepth 2 -name '*.kicad_symdir' -type d 2>/dev/null | while read -r d; do
        nick="$(basename "$d" .kicad_symdir)"
        add_entry "$SYM_TABLE" "$nick" "$d" "BLPL shared vendor library (read-only)"
    done
    find "$SHARED_DIR" -maxdepth 2 -name '*.pretty' -type d 2>/dev/null | while read -r d; do
        nick="$(basename "$d" .pretty)"
        add_entry "$FP_TABLE" "$nick" "$d" "BLPL shared vendor library (read-only)"
    done
fi
