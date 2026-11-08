#!/bin/bash
# Start the Skynet command server inside a Singularity / Apptainer sandbox.
#
# Adapted from Harbor 0.25.0 (Apache-2.0), singularity/bootstrap.sh. Changes:
# the server uses only the Python standard library, so this script does not
# make a venv or install pip packages. It installs python3 only when the
# image has none.
#
# Usage: singularity_bootstrap.sh WORKDIR [server arguments...]
set -u

WORKDIR="${1:-/app}"
shift
mkdir -p "$WORKDIR"
cd "$WORKDIR" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "[skynet] python3 not found, installing it" >&2
  export DEBIAN_FRONTEND=noninteractive
  if command -v apt-get >/dev/null 2>&1; then
    { apt-get update -qq && apt-get install -y -qq python3; } >&2 || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 >&2 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 >&2 || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 >&2 || true
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[skynet] FATAL: the image has no python3 and it could not be installed" >&2
    exit 1
  fi
fi

exec python3 "$(dirname "$0")/singularity_server.py" --workdir "$WORKDIR" "$@"
