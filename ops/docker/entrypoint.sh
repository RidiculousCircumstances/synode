#!/usr/bin/env sh
set -eu

if [ "${1:-}" = "serve" ]; then
  if [ "${SYNODE_SKIP_DB_UPGRADE:-0}" != "1" ]; then
    synode db upgrade
  fi
  exec synode serve --host 0.0.0.0 --port "${SYNODE_PORT:-8787}"
fi

if [ "${1:-}" = "worker" ]; then
  if [ "${SYNODE_SKIP_DB_UPGRADE:-0}" != "1" ]; then
    synode db upgrade
  fi
  exec synode worker run
fi

if [ "${1:-}" = "db-upgrade" ]; then
  exec synode db upgrade
fi

exec "$@"
