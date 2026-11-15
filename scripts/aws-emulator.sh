#!/usr/bin/env bash
# Starts, stops and connects to a local AWS Emulator in Docker (SKY-708).
# Read docs/agents/aws-emulator.md before you use it.
#
#   scripts/aws-emulator.sh start   Start the emulator. Do nothing when it already answers.
#   scripts/aws-emulator.sh stop    Stop the emulator. Succeed when none runs.
#   scripts/aws-emulator.sh env     Print the export and unset lines for the emulator.
#
# Connect in a subshell, so real AWS settings come back after it:
#   ( eval "$(scripts/aws-emulator.sh env)" && aws sts get-caller-identity )
#
# AWS_EMULATOR_PORT sets the port (default 9297). Each port is one emulator.
# Start builds the image when it is missing or older than 7 days, so it gets
# the latest release. AWS_EMULATOR_REBUILD=1 rebuilds it now.
# It needs Bash 3.2 or newer (the macOS system Bash), curl and Docker.

set -euo pipefail

port="${AWS_EMULATOR_PORT:-9297}"
image="skynet-aws-emulator"
name="aws-emulator-$port"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="$HOME/.aws/emulator/$port"
built_label="skynet.aws-emulator.built-at"
max_age=$((7 * 86400))

die() {
  echo "error: $*" >&2
  exit 1
}

if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then
  die "AWS_EMULATOR_PORT must be a number from 1024 to 65535"
fi

# The health check trusts only this emulator's own CA certificate.
healthy() {
  [[ -f "$dir/ca.pem" ]] || return 1
  [[ "$(curl -fsS --max-time 2 --cacert "$dir/ca.pem" "https://127.0.0.1:$port/__admin/health" 2>/dev/null)" == ok ]]
}

# A rebuild uses --pull --no-cache: the package URL never changes, so a
# cached layer would keep an old release.
build_image() {
  local built now
  built="$(docker image inspect -f "{{ index .Config.Labels \"$built_label\" }}" "$image" 2>/dev/null || true)"
  now="$(date +%s)"
  if [[ "${AWS_EMULATOR_REBUILD:-}" == 1 || ! "$built" =~ ^[0-9]+$ ]] || ((now - built > max_age)); then
    echo "building $image from the latest AWS Emulator release"
    docker build --pull --no-cache --label "$built_label=$now" -t "$image" "$root/deploy/aws-emulator" >&2 ||
      die "docker build failed"
  fi
}

running() {
  [[ "$(docker container inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" == true ]]
}

start() {
  local i
  command -v docker >/dev/null 2>&1 || die "missing command: docker"
  command -v curl >/dev/null 2>&1 || die "missing command: curl"
  if healthy; then
    echo "AWS Emulator already answers on https://127.0.0.1:$port"
    return 0
  fi
  if ! running; then
    build_image
    mkdir -p "$HOME/.aws/emulator"
    # The official container recipe, plus hardening. HOME is the same path in
    # the container, so the files that the emulator writes hold host paths.
    # Port publish on loopback only: other computers must never reach it.
    docker run -d --rm --name "$name" \
      --cap-drop ALL --security-opt no-new-privileges --read-only \
      --memory=2g --memory-swap=2g \
      -e EMULATOR_TELEMETRY=off -e HOME="$HOME" \
      -p "127.0.0.1:$port:$port" \
      -v "$HOME/.aws/emulator:$HOME/.aws/emulator" \
      "$image" -p "$port" --telemetry off >/dev/null || die "docker run failed (is port $port in use?)"
  fi
  for ((i = 0; i < 60; i++)); do
    if healthy; then
      echo "AWS Emulator answers on https://127.0.0.1:$port (container $name)"
      return 0
    fi
    running || die "container $name stopped during its start"
    sleep 1
  done
  docker logs --tail 20 "$name" >&2 || true
  die "AWS Emulator did not answer on port $port after 60 seconds"
}

stop() {
  command -v docker >/dev/null 2>&1 || die "missing command: docker"
  if docker container inspect "$name" >/dev/null 2>&1; then
    docker stop "$name" >/dev/null
    echo "stopped $name"
  else
    echo "no container $name runs"
  fi
}

# Prints the lines even when the files are missing. Then an eval still
# removes the real credentials and points at loopback, so a call fails
# instead of reaching real AWS.
print_env() {
  local var
  printf 'export AWS_ENDPOINT_URL=%q\n' "https://127.0.0.1:$port"
  printf 'export AWS_SHARED_CREDENTIALS_FILE=%q\n' "$dir/emulator_credentials"
  printf 'export AWS_CONFIG_FILE=%q\n' "$dir/config"
  printf 'export AWS_CA_BUNDLE=%q\n' "$dir/ca.pem"
  printf 'export AWS_PROFILE=default\n'
  printf 'export AWS_REGION=us-east-1\n'
  printf 'export AWS_DEFAULT_REGION=us-east-1\n'
  printf 'export AWS_EC2_METADATA_DISABLED=true\n'
  printf 'export AWS_PAGER=""\n'
  for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_DEFAULT_PROFILE \
    AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_ARN AWS_ROLE_SESSION_NAME \
    AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_CREDENTIALS_FULL_URI \
    AWS_CONTAINER_AUTHORIZATION_TOKEN; do
    printf 'unset %s\n' "$var"
  done
  [[ -f "$dir/emulator_credentials" && -f "$dir/ca.pem" ]] ||
    die "no emulator files in $dir: run scripts/aws-emulator.sh start first"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  env) print_env ;;
  *) die "usage: scripts/aws-emulator.sh start|stop|env" ;;
esac
