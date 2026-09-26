#!/usr/bin/env bash
# Make app/data exist and belong to the user the containers run as.
#
# Run it BEFORE the first `docker compose up`, and again once when a
# deployment first moves from running as root to running as BLPL_UID.
#
# Before first start, because Docker creates a missing bind-mount source
# itself, as root. The backend then runs as BLPL_UID inside a root-owned
# /app/data and cannot even write server.key. So this creates every bind
# source compose mounts from under data/ with the right owner first.
#
# After a move off root, because everything written before it is root:root
# and the new containers cannot write over it. After that it should never be
# needed; if it is, something is still running as root and that is the bug
# to find.
#
# Only app/data. The Postgres bind mount (app/blpl-db) belongs to the
# postgres image's own uid and must NOT be chowned to this one.
#
# Usage: app/fix-data-ownership.sh            (reads BLPL_UID/GID from app/.env)
#        app/fix-data-ownership.sh --dry-run  (lists what is missing or owned by anyone else)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data="$here/data"

# Every bind source docker-compose.yml mounts from under data/. A directory
# missing here is one Docker would create as root on first start.
bind_sources=("$data" "$data/ssh" "$data/kicad-desktop" "$data/projects" "$data/modules")

if [[ -f "$here/.env" ]]; then
    uid="$(sed -n 's/^BLPL_UID=//p' "$here/.env" | tail -1)"
    gid="$(sed -n 's/^BLPL_GID=//p' "$here/.env" | tail -1)"
fi
uid="${uid:-${BLPL_UID:-}}"
gid="${gid:-${BLPL_GID:-}}"
if [[ -z "$uid" || -z "$gid" ]]; then
    echo "BLPL_UID and BLPL_GID must be set in $here/.env (or the environment)" >&2
    exit 1
fi

if [[ "${1:-}" == "--dry-run" ]]; then
    for d in "${bind_sources[@]}"; do
        [[ -d "$d" ]] || echo "missing: $d"
    done
    # -uid/-gid rather than -user/-group: the numbers need not have names here.
    [[ -d "$data" ]] && find "$data" \( ! -uid "$uid" -o ! -gid "$gid" \) -print
    exit 0
fi

# sudo only when something needs it: creating as the invoking user is enough
# when that user is BLPL_UID and owns app/.
as_root=""
if [[ "$(id -u)" != "0" ]] && { [[ "$(id -u)" != "$uid" ]] || [[ ! -w "$here" ]]; }; then
    as_root="sudo"
fi

$as_root mkdir -p "${bind_sources[@]}"
if [[ -n "$(find "$data" \( ! -uid "$uid" -o ! -gid "$gid" \) -print -quit)" ]]; then
    $as_root chown -R "$uid:$gid" "$data" 2>/dev/null || sudo chown -R "$uid:$gid" "$data"
fi
echo "app/data ready, owned by $uid:$gid"
