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

# register_project <project-dir> <name>
# One writable symbol library and one writable .pretty, named after the
# project, in the layout every pipeline stage already indexes.
register_project() {
    local proj="$1" name="$2"
    local symdir="$proj/libraries/symbols"
    local fpdir="$proj/libraries/footprints"
    mkdir -p "$symdir" "$fpdir"
    local symfile="$symdir/$name.kicad_sym"
    if [ ! -s "$symfile" ]; then
        # A minimal valid, empty library: a file KiCad will open and save
        # into, and the flat layout every pipeline stage already indexes.
        printf '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "blpl")\n\t(generator_version "1.0")\n)\n' > "$symfile"
    fi
    mkdir -p "$fpdir/$name.pretty"
    add_entry "$SYM_TABLE" "$name" "$symfile" "BLPL project library (writable) — searched FIRST by the pipeline"
    add_entry "$FP_TABLE" "$name" "$fpdir/$name.pretty" "BLPL project library (writable) — searched FIRST by the pipeline"
}

# --- one writable library per project ---------------------------------------
#
# Two mount shapes, because the compose file has two deployment modes:
#
#   KICAD_DESKTOP_PROJECT unset -> ./data/projects      is /config/projects,
#                                  so each CHILD is a project.
#   KICAD_DESKTOP_PROJECT=name  -> ./data/projects/name is /config/projects,
#                                  so the ROOT is the project.
#
# Only the first was handled, and the second is the mode a deployment with
# accounts is told to use. There the loop walked `core/`, `sb-ble/`,
# `design-notes/` as though each were a project, found no `libraries/` in any
# of them, and registered nothing at all — while the project's real
# `libraries/` sat unregistered one level up. The desktop then offered only
# the read-only stock mount and PCM installs: precisely the "draw the missing
# symbol and lose it" failure this script exists to prevent, in the one mode
# where it was never noticed because the single-operator default works.
#
# The root layout is detected rather than inferred from the variable, because
# the variable lives in the backend's environment and this container should
# not have to agree with it to be correct.
if [ -d "$PROJECTS_DIR/libraries" ]; then
    name="${KICAD_DESKTOP_PROJECT:-}"
    case "$name" in
        ""|none) name="$(basename "$(readlink -f "$PROJECTS_DIR")")" ;;
    esac
    # A bind mount's own basename is "projects" when the source is anonymous;
    # anything is better than registering a library called that.
    [ "$name" = "projects" ] && name="project"
    echo "kicad-init: single-project mount detected, registering root as '$name'"
    register_project "$PROJECTS_DIR" "$name"
else
    for proj in "$PROJECTS_DIR"/*/; do
        [ -d "$proj" ] || continue
        proj="${proj%/}"
        # Only projects that have the libraries/ layout (blpl init creates it).
        [ -d "$proj/libraries" ] || continue
        register_project "$proj" "$(basename "$proj")"
    done
fi

# --- stock symbols: repair the registration the mount breaks ----------------
# The image's template sym-lib-table lists the flat <Lib>.kicad_sym files the
# Alpine kicad-library package installs — and the compose mount replaces
# /usr/share/kicad/symbols with the fork's one-file-per-symbol .kicad_symdir
# layout, so every one of those entries dangles and the symbol editor shows
# no stock library at all. (Footprints never had the problem: .pretty
# directories are the same layout in both.) When the mounted layout is the
# symdir one, generate a table that names what is actually there and repoint
# the global table's "KiCad" include row at it. Regenerated every start, so
# the registration tracks the fork as it grows.
STOCK_SYMBOLS="/usr/share/kicad/symbols"
stock_table="$KICAD_CONFIG_DIR/$ver/blpl-stock-sym-lib-table"
if ls "$STOCK_SYMBOLS"/*.kicad_symdir >/dev/null 2>&1; then
    {
        printf '(sym_lib_table\n\t(version 7)\n'
        for d in "$STOCK_SYMBOLS"/*.kicad_symdir; do
            printf '\t(lib (name "%s") (type "KiCad") (uri "%s") (options "") (descr "stock symbols (read-only)"))\n' \
                "$(basename "$d" .kicad_symdir)" "$d"
        done
        printf ')\n'
    } > "$stock_table"
    sed -i 's|(lib (name "KiCad") (type "Table") (uri "[^"]*")|(lib (name "KiCad") (type "Table") (uri "'"$stock_table"'")|' "$SYM_TABLE"
    if ! grep -qF "$stock_table" "$SYM_TABLE"; then
        # No include row to repoint (a fresh table this script created):
        # add one. Type "Table" — a nested table include, not a library.
        tmp="$SYM_TABLE.blpl-tmp"
        sed '$d' "$SYM_TABLE" > "$tmp"
        printf '\t(lib (name "KiCad") (type "Table") (uri "%s") (options "") (descr "KiCad Default Libraries"))\n)\n' \
            "$stock_table" >> "$tmp"
        mv "$tmp" "$SYM_TABLE"
    fi
    echo "kicad-init: stock symbol table -> $stock_table ($(grep -c '(lib ' "$stock_table") libraries)"
fi

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
