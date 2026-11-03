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
        ;;
      *batch-get-image*)
        printf '%s\n' '{"images":[{"imageManifest":"{\"schemaVersion\":2}","imageManifestMediaType":"application/vnd.oci.image.manifest.v1+json"}]}'
        ;;
      *put-image*)
        [[ "$*" == *"--image-manifest-media-type application/vnd.oci.image.manifest.v1+json"* ]] || exit 3
        if [[ "${RETAG_FAIL:-}" == true ]]; then
          printf '%s\n' 'retag failed' >&2
          exit 254
        fi
        if [[ "${RETAG_EXISTS:-}" == true ]]; then
          printf '%s\n' 'ImageAlreadyExistsException' >&2
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
printf '%s\n' 'image-key tests passed'
