#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../../../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"
cat >"$tmp/bin/stub" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "${0##*/}" in
  git)
    if [[ "$1" == -C ]]; then shift 2; fi
    case "$1" in
      rev-parse)
        if [[ "$2" == --show-toplevel ]]; then
          printf '%s\n' "$TEST_REPO_ROOT"
        else
          printf '%s\n' "${TREE_HASH:-tree-one}"
        fi
        ;;
      status)
        [[ "$*" == *--ignored* ]] || exit 4
        printf '%s' "${DIRTY_TREE:-}"
        ;;
      *) exit 2 ;;
    esac
    ;;
  docker)
    [[ "${DIGEST_FAIL:-}" != true ]] || exit 1
    printf '"%s"\n' "${BASE_DIGEST:-sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
    ;;
  aws)
    case "$*" in
      *describe-images*)
        if [[ "${DESCRIBE_ERROR:-}" == true ]]; then
          printf '%s\n' 'AccessDeniedException' >&2
          exit 254
        fi
        if [[ "$*" == *"--query imageDetails[0].imagePushedAt"* ]]; then
          printf '%s\n' "$PUSHED_AT"
          exit 0
        fi
        if [[ "$*" == *"--query imageDetails[0].imageDigest"* ]]; then
          if [[ "${RETAG_DIFF:-}" == true ]]; then
            printf '%s\n' 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
          else
            printf '%s\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
          fi
          exit 0
        fi
        if [[ "${LATEST_CACHE_TEST:-}" == true ]]; then
          [[ "${CACHE_ERROR:-}" != true ]] || exit 254
          if [[ "$*" == *"--starting-token page2"* ]]; then
            printf '%s\n' '{"imageDetails":[{"imageTags":["backend-old"],"imagePushedAt":"2025-01-01T00:00:00Z"}]}'
          else
            printf '%s\n' '{"imageDetails":[{"imageTags":["backend-new"],"imagePushedAt":"2025-02-01T00:00:00Z"}],"nextToken":"page2"}'
          fi
          exit 0
        fi
        if [[ "${RETAG_DIFF:-}" == true ]]; then
          printf '%s\n' '{"imageDetails":[{"imageDigest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}]}'
          exit 0
        fi
        if [[ "${FRESHNESS_TEST:-}" == true ]]; then
          printf '{"imageDetails":[{"imagePushedAt":"%s"}]}\n' "$PUSHED_AT"
          exit 0
        fi
        printf '%s\n' '{"imageDetails":[{"imageDigest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}'
        ;;
      *batch-get-image*)
        printf '%s\n' '{"images":[{"imageId":{"imageDigest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"imageManifest":"{\"schemaVersion\":2}","imageManifestMediaType":"application/vnd.oci.image.manifest.v1+json"}]}'
        ;;
      *put-image*)
        [[ "$*" == *"--image-manifest-media-type application/vnd.oci.image.manifest.v1+json"* ]] || exit 3
        if [[ "${RETAG_FAIL:-}" == true ]]; then
          printf '%s\n' 'retag failed' >&2
          exit 254
        fi
        if [[ "${RETAG_EXISTS:-}" == true ]]; then
          printf '%s\n' "${RETAG_ERROR:-ImageAlreadyExistsException}" >&2
          exit 254
        fi
        ;;
      *) exit 2 ;;
    esac
    ;;
esac
SH
chmod +x "$tmp/bin/stub"
for command in aws docker git; do ln -s stub "$tmp/bin/$command"; done
export PATH="$tmp/bin:$PATH"
export TEST_REPO_ROOT="$repo_root"
export TREE_HASH BASE_DIGEST DIRTY_TREE DIGEST_FAIL DESCRIBE_ERROR RETAG_FAIL RETAG_EXISTS
export RETAG_ERROR RETAG_DIFF LATEST_CACHE_TEST CACHE_ERROR FRESHNESS_TEST PUSHED_AT
source "$repo_root/infra/terraform/scripts/image-key.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

key="$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=on)"
[[ "$key" =~ ^ctx-[0-9a-f]{40}$ ]] || fail "key format: $key"
[[ "$key" == "$(image_key "$repo_root/backend" ctx FEATURE=on GIT_SHA=abc)" ]] || fail "input order changed key"
tree_key="$(image_key "$repo_root/backend" tree GIT_SHA=abc FEATURE=on)"
[[ "$tree_key" =~ ^tree-[0-9a-f]{40}$ ]] || fail "tree key format: $tree_key"
[[ "$tree_key" == "$(image_key "$repo_root/backend" tree GIT_SHA=def FEATURE=off)" ]] ||
  fail "tree key changed with build args"
TREE_HASH=tree-two
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=on)" ]] || fail "tree change kept key"
TREE_HASH=tree-one
BASE_DIGEST=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=on)" ]] || fail "base digest change kept key"
unset BASE_DIGEST
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=off)" ]] || fail "build arg change kept key"
DIRTY_TREE=' M backend/Dockerfile'
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "dirty tree produced key"
DIRTY_TREE='!! backend/ignored-file'
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "ignored file produced key"
unset DIRTY_TREE
DIGEST_FAIL=true
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "unresolved base produced key"
unset DIGEST_FAIL
DESCRIBE_ERROR=true
if find_by_key skynet/backend "$key"; then fail "AccessDenied looked like a hit"; fi
unset DESCRIBE_ERROR
RETAG_FAIL=true
if retag skynet/backend "$key" sha; then fail "retag failure succeeded"; fi
unset RETAG_FAIL
RETAG_EXISTS=true
retag skynet/backend "$key" sha || fail "existing retag failed"
RETAG_ERROR=ImageTagAlreadyExistsException
RETAG_EXISTS=true
retag skynet/backend "$key" sha || fail "tag-exists retag failed"
RETAG_DIFF=true
if retag skynet/backend "$key" sha; then fail "different ImageTagAlreadyExists digest succeeded"; fi
RETAG_ERROR=ImageAlreadyExistsException
if retag skynet/backend "$key" sha; then fail "different ImageAlreadyExists digest succeeded"; fi
unset RETAG_EXISTS RETAG_ERROR
unset RETAG_DIFF
LATEST_CACHE_TEST=true
[[ "$(latest_cache_ref skynet/cache backend-)" == backend-new ]] || fail "latest cache tag lookup failed"
CACHE_ERROR=true
[[ -z "$(latest_cache_ref skynet/cache backend-)" ]] || fail "cache lookup error was not ignored"
unset LATEST_CACHE_TEST CACHE_ERROR
FRESHNESS_TEST=true
PUSHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
find_fresh_by_key skynet/backend "$key" || fail "fresh keyed image was not reused"
PUSHED_AT=2000-01-01T00:00:00Z
if find_fresh_by_key skynet/backend "$key"; then fail "stale keyed image was reused"; fi
unset FRESHNESS_TEST PUSHED_AT

test_context="$repo_root/.image-key-test-$BASHPID"
mkdir -p "$test_context"
trap 'rm -rf "$test_context"; rm -rf "$tmp"' EXIT
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine AS base
FROM base AS final
COPY --from=final /etc/os-release /os-release
DOCKERFILE
ignored_key="$(image_key "$test_context" ctx GIT_SHA=abc)"
[[ "$ignored_key" =~ ^ctx-[0-9a-f]{40}$ ]] || fail "valid context key: $ignored_key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM --platform=linux/arm64 docker.io/node:22.22.0-bookworm-slim AS node22
FROM node:20-alpine AS node20
FROM scratch
COPY --from=node22 /etc/os-release /node22
COPY --from=node20 /etc/os-release /node20
DOCKERFILE
[[ "$(image_key "$test_context" ctx GIT_SHA=abc)" =~ ^ctx-[0-9a-f]{40}$ ]] ||
  fail "platform FROM alias offsets produced no key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
# syntax=docker/dockerfile:1
FROM node:20-alpine
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "syntax directive produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine \
  AS broken
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "continued FROM produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
COPY --from=missing /etc/os-release /os-release
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "unknown COPY stage produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
ADD --from=missing /etc/os-release /os-release
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "unknown ADD stage produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
COPY --from missing /etc/os-release /os-release
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "unknown spaced COPY stage produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
RUN --mount=type=bind,from=missing true
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "unknown RUN mount stage produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
ADD https://example.invalid/source /source
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "network ADD produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
ADD git@github.com:owner/repo.git /source
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "Git ADD produced key"
printf '%s\n' 'image-key tests passed'
