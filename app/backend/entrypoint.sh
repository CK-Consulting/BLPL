#!/bin/sh
# Bring the schema up to date, then hand off to the server.
#
# A script rather than a compose `command: sh -c "alembic upgrade head && uvicorn …"`
# for two reasons. First, `exec` at the end means uvicorn becomes PID 1, so
# docker stop's SIGTERM reaches it and a run in flight gets a clean shutdown
# instead of a ten-second kill. Second, the migration failing must stop the
# container — with `&&` inside a shell that is easy to get wrong, and a server
# that boots against a schema it does not match produces confusing errors far
# from the cause.
#
# `depends_on: condition: service_healthy` in compose means Postgres is already
# accepting connections by the time this runs, so there is no retry loop here on
# purpose: if the database is genuinely unreachable, failing loudly beats
# spinning quietly.
set -e

# The image was built with a passwd entry for one uid (BLPL_UID build arg) and
# compose runs it as whatever `user:` says. If those differ, ssh remotes fail
# much later with "No user exists for uid", far from the cause. Say it here.
if ! id -un >/dev/null 2>&1; then
    echo "entrypoint: uid $(id -u) has no passwd entry in this image;" \
         "rebuild with the same BLPL_UID/BLPL_GID compose runs it as" >&2
    exit 1
fi

echo "entrypoint: applying migrations"
alembic upgrade head

echo "entrypoint: starting $*"
exec "$@"
