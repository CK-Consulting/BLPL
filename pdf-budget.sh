#!/usr/bin/env bash
#
# What a set of datasheets costs to read, plus the text itself.
#
# Wraps blpl.agent.tools.pdf_budget so it can be run from anywhere against
# anything, and writes the extracted text beside you rather than leaving it
# inside a Python process. That is the point of running it by hand: the table
# tells you whether a document fits, and the text tells you whether the table
# it contains survived extraction — which is the question that actually decides
# whether a pinout can be pulled out of it.
#
#   ./pdf-budget.sh datasheets/*.pdf
#   ./pdf-budget.sh --context 32768 --context 512000 datasheets/
#   OUT=/tmp/text ./pdf-budget.sh some.pdf
#
# Text lands in ./pdf-text/<name>.txt unless OUT says otherwise. Everything
# after the options is passed through to the Python module unchanged.
set -euo pipefail

OUT="${OUT:-./pdf-text}"

# The venv inside the container is the one with the project installed, but this
# script is for running on a workstation — so prefer whatever python can import
# the package, and say something useful when none can.
find_python() {
  for candidate in "${PYTHON:-}" python3 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c "import blpl.agent.tools.pdf_budget" >/dev/null 2>&1; then
      echo "$candidate"; return 0
    fi
  done
  # Not importable anywhere: fall back to running the file directly, which
  # works from a checkout without the package installed.
  for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 && { echo "$candidate"; return 0; }
  done
  return 1
}

PY="$(find_python)" || { echo "pdf-budget: no python found on PATH" >&2; exit 127; }

command -v pdftotext >/dev/null 2>&1 || {
  echo "pdf-budget: pdftotext is not installed (Debian/Ubuntu: poppler-utils, Arch: poppler)" >&2
  exit 127
}

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ARGS=()
PDFS=()

# Split the arguments: paths get their text dumped, options are passed through.
while [ $# -gt 0 ]; do
  case "$1" in
    -*) MODULE_ARGS+=("$1")
        # Options that take a value carry it along.
        case "$1" in
          --context|--reserve|--first|--last) shift; MODULE_ARGS+=("$1");;
        esac
        ;;
    *)  PDFS+=("$1"); MODULE_ARGS+=("$1");;
  esac
  shift
done

[ ${#PDFS[@]} -gt 0 ] || { echo "usage: pdf-budget.sh [--context N]... <pdf|dir>..." >&2; exit 2; }

# Expand directories the same way the module does, so the text output and the
# table describe the same set of files.
FILES=()
for p in "${PDFS[@]}"; do
  if [ -d "$p" ]; then
    while IFS= read -r f; do FILES+=("$f"); done < <(find "$p" -maxdepth 1 -type f -iname '*.pdf' | sort)
  else
    FILES+=("$p")
  fi
done

mkdir -p "$OUT"
for f in "${FILES[@]}"; do
  base="$(basename "${f%.*}")"
  # -layout, because a pin table is columns and the whitespace is the structure.
  # Losing it is losing the table.
  if pdftotext -layout "$f" "$OUT/$base.txt" 2>/dev/null; then
    printf 'text  %-52s -> %s\n' "$(basename "$f")" "$OUT/$base.txt"
  else
    printf 'text  %-52s -> FAILED\n' "$(basename "$f")" >&2
  fi
done
echo

if "$PY" -c "import blpl.agent.tools.pdf_budget" >/dev/null 2>&1; then
  "$PY" -m blpl.agent.tools.pdf_budget "${MODULE_ARGS[@]}"
else
  "$PY" "$HERE/blpl/agent/tools/pdf_budget.py" "${MODULE_ARGS[@]}"
fi
