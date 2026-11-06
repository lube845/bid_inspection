#!/bin/sh
set -e

if [ "${RUN_BOOTSTRAP:-0}" = "1" ]; then
  python -m app.bootstrap
fi

exec "$@"
