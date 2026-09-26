#!/usr/bin/env bash
# Give app/data back to the user the containers run as.
#
# Needed once, when a deployment first moves from running as root to running
# as BLPL_UID: everything written before that is root:root, and the new
# containers cannot write over it. After that it should never be needed; if
# it is, something is still running as root and that is the bug to find.
#
# Only app/data. The Postgres bind mount (app/blpl-db) belongs to the
# postgres image's own uid and must NOT be chowned to this one.
#
# Usage: app/fix-data-ownership.sh            (reads BLPL_UID/GID from app/.env)
#        app/fix-data-ownership.sh --dry-run  (lists what is owned by anyone else)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data="$here/data"

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
if [[ ! -d "$data" ]]; then
    echo "no $data to fix" >&2
    exit 1
fi

# -uid/-gid rather than -user/-group: the numbers need not have names here.
if [[ "${1:-}" == "--dry-run" ]]; then
    find "$data" \( ! -uid "$uid" -o ! -gid "$gid" \) -print
    exit 0
fi

sudo chown -R "$uid:$gid" "$data"
echo "app/data now owned by $uid:$gid"
