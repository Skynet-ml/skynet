#!/usr/bin/env bash
set -euo pipefail

for command in awk grep git jq sed; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'missing test command: %s\n' "$command" >&2
    exit 1
  }
done
command -v python3 >/dev/null 2>&1 || {
  printf 'missing test command: python3\n' >&2
  exit 1
}
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || {
  printf 'missing test command: sha256sum or shasum\n' >&2
  exit 1
}

repo_root="$(cd "$(dirname "$0")/../../../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"
cat >"$tmp/bin/stub" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "${0##*/}" in
  git)
    git_root=""
    if [[ "$1" == -C ]]; then git_root="$2"; shift 2; fi
    case "$1" in
      rev-parse)
        if [[ "$2" == --show-toplevel ]]; then
          if [[ "$git_root" == "$TEST_ALT_REPO_ROOT"* ]]; then
            printf '%s\n' "$TEST_ALT_REPO_ROOT"
          else
            printf '%s\n' "$TEST_REPO_ROOT"
          fi
        else
          printf '%s\n' "${TREE_HASH:-tree-one}"
        fi
        ;;
      status)
        [[ "$*" == *--ignored* ]] || exit 4
        printf '%s' "${DIRTY_TREE:-}"
        ;;
      config)
        case "${*: -1}" in
          core.autocrlf)
            [[ -n "${TEST_CORE_AUTOCRLF:-}" ]] || exit 1
            printf '%s\n' "$TEST_CORE_AUTOCRLF"
            ;;
          core.eol)
            [[ -n "${TEST_CORE_EOL:-}" ]] || exit 1
            printf '%s\n' "$TEST_CORE_EOL"
            ;;
          core.sparseCheckout)
            [[ "${TEST_SPARSE_CHECKOUT:-}" == true ]] || exit 1
            printf 'true\n'
            ;;
          core.symlinks)
            [[ -n "${TEST_CORE_SYMLINKS:-}" ]] || exit 1
            printf '%s\n' "$TEST_CORE_SYMLINKS"
            ;;
          core.fileMode)
            [[ -n "${TEST_CORE_FILEMODE:-}" ]] || exit 1
            printf '%s\n' "$TEST_CORE_FILEMODE"
            ;;
          *) exit 2 ;;
        esac
        ;;
      ls-files)
        context="${*: -1}"
        if [[ "$*" == *" -v "* ]]; then
          printf '%s %s\n' "${TEST_INDEX_FLAG:-H}" "$context/Dockerfile"
          exit 0
        fi
        printf '%s\0' "$context/Dockerfile"
        ;;
      check-attr)
        [[ "${ATTR_FAIL:-}" != true ]] || exit 11
        while IFS= read -r -d '' path; do
          for attr in filter eol text ident working-tree-encoding; do
            value=unspecified
            if [[ "$path" == "${FILTER_ATTR_PATH:-}" && "$attr" == "${FILTER_ATTR_NAME:-}" ]]; then
              value="${FILTER_ATTR_VALUE:-lfs}"
            fi
            printf '%s\0%s\0%s\0' "$path" "$attr" "$value"
          done
        done
        ;;
      *) exit 2 ;;
    esac
    ;;
  docker)
    if [[ "$1" == buildx && "$2" == version ]]; then
      printf 'github.com/docker/buildx %s\n' "${BUILDX_VERSION:-v0.36.1}"
    elif [[ "$1" == buildx && "$2" == inspect ]]; then
      printf 'Driver: %s\nBuildKit version: %s\n' \
        "${BUILDKIT_DRIVER:-docker-container}" "${BUILDKIT_VERSION:-v0.32.2}"
    elif [[ "$1" == buildx && "$2" == imagetools ]]; then
      [[ "${DIGEST_FAIL:-}" != true ]] || exit 1
      [[ -n "${DIGEST_CALLS_OUT:-}" ]] && printf '%s\n' call >>"$DIGEST_CALLS_OUT"
      printf '"%s"\n' "${BASE_DIGEST:-sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
    else
      exit 2
    fi
    ;;
  date)
    if [[ "${DATE_PARSE_FAIL:-}" == true && "$1" == -d ]]; then exit 1; fi
    exec /usr/bin/date "$@"
    ;;
  curl)
    [[ "${CURL_FAIL:-}" != true ]] || exit 1
    header_file=""
    for arg in "$@"; do
      [[ "$arg" != *request-token-secret* ]] || exit 5
      [[ "$arg" == @* ]] && header_file="${arg#@}"
    done
    [[ -n "$header_file" && "$(stat -c '%a' "$header_file")" == 600 ]] || exit 6
    [[ "$(cat "$header_file")" == 'Authorization: bearer request-token-secret' ]] || exit 7
    [[ -z "${CREDENTIAL_REFRESH_COUNT_OUT:-}" ]] ||
      printf '%s\n' refresh >>"$CREDENTIAL_REFRESH_COUNT_OUT"
    printf '%s\n' "$header_file" >"$OIDC_HEADER_PATH_OUT"
    printf '%s\n' '{"value":"oidc-token-secret"}'
    ;;
  aws)
    case "$*" in
      *"sts assume-role-with-web-identity"*)
        token_file=""
        for arg in "$@"; do
          [[ "$arg" != *oidc-token-secret* ]] || exit 8
          [[ "$arg" == file://* ]] && token_file="${arg#file://}"
        done
        [[ -n "$token_file" && "$(stat -c '%a' "$token_file")" == 600 ]] || exit 9
        [[ "$(cat "$token_file")" == oidc-token-secret ]] || exit 10
        printf '%s\n' "$token_file" >"$OIDC_TOKEN_PATH_OUT"
        printf '%s\n' '{"Credentials":{"AccessKeyId":"test-access","SecretAccessKey":"test-secret","SessionToken":"test-session"}}'
        ;;
      *"ssm get-parameter"*)
        printf '%s\n' '{"cluster":"test-cluster"}'
        ;;
      *"ecs describe-tasks"*)
        case "$*" in
          *"tasks[0].lastStatus"*) printf '%s\n' STOPPED ;;
          *"containers[0].exitCode"*) printf '%s\n' 0 ;;
          *"tasks[0].stoppedReason"*) printf '%s\n' test ;;
          *) exit 12 ;;
        esac
        ;;
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
            printf '%s\n' '{"imageDetails":[{"imageTags":["backend-2026-W02-old"],"imagePushedAt":"2025-01-01T00:00:00Z"}]}'
          else
            printf '%s\n' '{"imageDetails":[{"imageTags":["backend-2026-W02-new","backend-2026-W01-new"],"imagePushedAt":"2025-02-01T00:00:00Z"}],"nextToken":"page2"}'
          fi
          exit 0
        fi
        if [[ "${CACHE_TIMESTAMP_TEST:-}" == true ]]; then
          printf '%s\n' '{"imageDetails":[{"imageTags":["backend-2026-W02-offset-new"],"imagePushedAt":"2025-02-01T00:00:00.500-08:00"},{"imageTags":["backend-2026-W02-utc-old"],"imagePushedAt":"2025-02-01T01:00:00+00:00"}]}'
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
for command in aws curl date docker git; do ln -s stub "$tmp/bin/$command"; done
export PATH="$tmp/bin:$PATH"
export TEST_REPO_ROOT="$repo_root"
TEST_ALT_REPO_ROOT="$tmp/another-checkout"
export TEST_ALT_REPO_ROOT
export TREE_HASH BASE_DIGEST DIRTY_TREE DIGEST_FAIL DESCRIBE_ERROR RETAG_FAIL RETAG_EXISTS
export RETAG_ERROR RETAG_DIFF LATEST_CACHE_TEST CACHE_ERROR FRESHNESS_TEST PUSHED_AT
export CACHE_TIMESTAMP_TEST TEST_CORE_AUTOCRLF TEST_CORE_EOL TEST_CORE_SYMLINKS TEST_CORE_FILEMODE
export TEST_SPARSE_CHECKOUT TEST_INDEX_FLAG
export BUILDX_VERSION BUILDKIT_VERSION DATE_PARSE_FAIL OIDC_HEADER_PATH_OUT OIDC_TOKEN_PATH_OUT
OIDC_HEADER_PATH_OUT="$tmp/oidc-header-path"
OIDC_TOKEN_PATH_OUT="$tmp/oidc-token-path"
DIGEST_CALLS_OUT="$tmp/digest-calls"
export OIDC_HEADER_PATH_OUT OIDC_TOKEN_PATH_OUT DIGEST_CALLS_OUT
CREDENTIAL_REFRESH_COUNT_OUT="$tmp/credential-refresh-count"
export CREDENTIAL_REFRESH_COUNT_OUT
GITHUB_ACTIONS=true
export GITHUB_ACTIONS
# shellcheck source=infra/terraform/scripts/image-key.sh
source "$repo_root/infra/terraform/scripts/image-key.sh"
# shellcheck source=infra/terraform/scripts/lib.sh
source "$repo_root/infra/terraform/scripts/lib.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

key="$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=on)"
[[ "$key" =~ ^ctx-[0-9a-f]{40}$ ]] || fail "key format: $key"
[[ "$key" == "$(image_key "$repo_root/backend" ctx FEATURE=on GIT_SHA=abc)" ]] || fail "input order changed key"
tree_key="$(image_key "$repo_root/backend" tree GIT_SHA=abc FEATURE=on)"
[[ "$tree_key" =~ ^tree-[0-9a-f]{40}$ ]] || fail "tree key format: $tree_key"
[[ "$tree_key" == "$(image_key "$repo_root/backend" tree GIT_SHA=def FEATURE=on)" ]] ||
  fail "tree key changed with GIT_SHA"
[[ "$tree_key" != "$(image_key "$repo_root/backend" tree GIT_SHA=abc FEATURE=off)" ]] ||
  fail "tree key ignored a non-GIT_SHA build arg"
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=def FEATURE=on)" ]] || fail "ctx key ignored GIT_SHA"
TREE_HASH=tree-two
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=on)" ]] || fail "tree change kept key"
TREE_HASH=tree-one
BASE_DIGEST=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=on)" ]] || fail "base digest change kept key"
unset BASE_DIGEST
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=off)" ]] || fail "build arg change kept key"
BUILDX_VERSION=v0.36.2
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=on)" ]] || fail "buildx version change kept key"
unset BUILDX_VERSION
BUILDKIT_VERSION=v0.32.3
[[ "$key" != "$(image_key "$repo_root/backend" ctx GIT_SHA=abc FEATURE=on)" ]] || fail "BuildKit version change kept key"
unset BUILDKIT_VERSION
image_inputs "$repo_root/backend" 2026-W01 GIT_SHA=abc FEATURE=on
week_one="$IMAGE_KEY_CTX"
image_inputs "$repo_root/backend" 2026-W02 GIT_SHA=abc FEATURE=on
[[ "$week_one" != "$IMAGE_KEY_CTX" ]] || fail "week change kept key"
DIRTY_TREE=' M backend/Dockerfile'
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "dirty tree produced key"
DIRTY_TREE='!! backend/ignored-file'
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "ignored file produced key"
unset DIRTY_TREE
TEST_CORE_AUTOCRLF=true
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "non-default core.autocrlf produced key"
unset TEST_CORE_AUTOCRLF
TEST_CORE_EOL=crlf
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "non-default core.eol produced key"
unset TEST_CORE_EOL
TEST_CORE_SYMLINKS=false
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "core.symlinks=false produced key"
unset TEST_CORE_SYMLINKS
TEST_CORE_FILEMODE=false
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "core.fileMode=false produced key"
unset TEST_CORE_FILEMODE
TEST_INDEX_FLAG=h
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "assume-unchanged file produced key"
TEST_INDEX_FLAG=S
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "skip-worktree file produced key"
unset TEST_INDEX_FLAG
TEST_SPARSE_CHECKOUT=true
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "sparse checkout produced key"
unset TEST_SPARSE_CHECKOUT
DOCKER_BUILDKIT=0
[[ -z "$(image_key "$repo_root/backend" ctx GIT_SHA=abc)" ]] || fail "DOCKER_BUILDKIT=0 produced key"
unset DOCKER_BUILDKIT
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
[[ "$(latest_cache_ref skynet/cache backend-2026-W02-)" == backend-2026-W02-new ]] ||
  fail "latest current-week cache tag lookup failed"
unset LATEST_CACHE_TEST
CACHE_TIMESTAMP_TEST=true
[[ "$(latest_cache_ref skynet/cache backend-2026-W02-)" == backend-2026-W02-offset-new ]] ||
  fail "cache timestamps were sorted as strings"
CACHE_TIMESTAMP_TEST=false
LATEST_CACHE_TEST=true
CACHE_ERROR=true
[[ -z "$(latest_cache_ref skynet/cache backend-)" ]] || fail "cache lookup error was not ignored"
unset LATEST_CACHE_TEST CACHE_ERROR
FRESHNESS_TEST=true
PUSHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATE_PARSE_FAIL=true
find_fresh_by_key skynet/backend "$key" || fail "fresh keyed image was not reused"
unset DATE_PARSE_FAIL
PUSHED_AT=2000-01-01T00:00:00Z
if find_fresh_by_key skynet/backend "$key"; then fail "stale keyed image was reused"; fi
unset FRESHNESS_TEST PUSHED_AT
IMAGE_PINNED_DIGESTS=(sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
printf '%s\n' '{"buildx.build.provenance":{"materials":[{"uri":"pkg:docker/node?digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","digest":{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}]}}' \
  >"$tmp/metadata.json"
verify_image_sources "$tmp/metadata.json" || fail "pinned source digest was not verified"
printf '%s\n' '{"buildx.build.provenance":{"materials":[{"uri":"pkg:docker/node?digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","digest":{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},{"uri":"file:///context","digest":{"sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}]}}' \
  >"$tmp/metadata.json"
verify_image_sources "$tmp/metadata.json" || fail "recognized local-file material was not verified"
printf '%s\n' '{"buildx.build.provenance":{"materials":[]}}' >"$tmp/metadata.json"
if verify_image_sources "$tmp/metadata.json"; then fail "missing source digest was verified"; fi
printf '%s\n' '{"buildx.build.provenance":{"materials":[{"uri":"pkg:docker/node?digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","digest":{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},{"uri":"pkg:docker/unknown?digest=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","digest":{"sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}]}}' \
  >"$tmp/metadata.json"
if verify_image_sources "$tmp/metadata.json"; then fail "unpinned Docker source was verified"; fi
for uri in \
  pkg:oci/node@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  docker-image://node@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  unknown:node; do
  printf '{"buildx.build.provenance":{"materials":[{"uri":"%s","digest":{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}]}}\n' \
    "$uri" >"$tmp/metadata.json"
  if verify_image_sources "$tmp/metadata.json"; then
    fail "unrecognized provenance material was verified: $uri"
  fi
done
unset IMAGE_PINNED_DIGESTS

export ACTIONS_ID_TOKEN_REQUEST_URL="https://token.invalid/request?value=1"
export ACTIONS_ID_TOKEN_REQUEST_TOKEN=request-token-secret
export AWS_DEPLOY_ROLE_ARN=arn:aws:iam::123456789012:role/test
refresh_aws_credentials
[[ "$AWS_ACCESS_KEY_ID" == test-access && "$AWS_SECRET_ACCESS_KEY" == test-secret &&
  "$AWS_SESSION_TOKEN" == test-session ]] || fail "OIDC credentials were not installed"
[[ -n "${AWS_CREDENTIALS_REFRESHED_AT:-}" ]] || fail "credential refresh timestamp was not exported"
bash -c '[[ -n "${AWS_CREDENTIALS_REFRESHED_AT:-}" ]]' ||
  fail "credential refresh timestamp was not inherited by child processes"
[[ ! -e "$(cat "$OIDC_HEADER_PATH_OUT")" && ! -e "$(cat "$OIDC_TOKEN_PATH_OUT")" ]] ||
  fail "OIDC temp files were not removed"
unset ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN AWS_DEPLOY_ROLE_ARN

export ACTIONS_ID_TOKEN_REQUEST_URL=https://token.invalid/request
export ACTIONS_ID_TOKEN_REQUEST_TOKEN=request-token-secret
export AWS_ACCESS_KEY_ID=action-access AWS_SECRET_ACCESS_KEY=action-secret AWS_SESSION_TOKEN=action-session
notice="$(refresh_aws_credentials 2>&1)"
[[ "$notice" == "notice: keeping GitHub action-provided AWS credentials; no deploy role is set" ]] ||
  fail "missing-role OIDC config did not emit one notice"
[[ "$AWS_ACCESS_KEY_ID" == action-access && "$AWS_SECRET_ACCESS_KEY" == action-secret ]] ||
  fail "missing role replaced action-provided credentials"
if (unset ACTIONS_ID_TOKEN_REQUEST_TOKEN; refresh_aws_credentials >/dev/null 2>&1); then
  fail "partial OIDC configuration succeeded"
fi
# shellcheck disable=SC2030
if (
  unset ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN
  AWS_DEPLOY_ROLE_ARN=arn:aws:iam::123456789012:role/test
  refresh_aws_credentials >/dev/null 2>&1
); then
  fail "role-only credential configuration succeeded"
fi
# shellcheck disable=SC2031
export AWS_DEPLOY_ROLE_ARN=arn:aws:iam::123456789012:role/test
AWS_CREDENTIALS_REFRESHED_AT=0
: >"$CREDENTIAL_REFRESH_COUNT_OUT"
wait_task_ok staging arn:aws:ecs:us-east-2:123456789012:task/test
[[ "$(wc -l <"$CREDENTIAL_REFRESH_COUNT_OUT")" -eq 1 ]] ||
  fail "migration poll did not refresh expired AWS credentials"
now="$(date +%s)"
: >"$CREDENTIAL_REFRESH_COUNT_OUT"
AWS_CREDENTIALS_REFRESHED_AT=$((now - 45 * 60 + 5))
wait_task_ok staging arn:aws:ecs:us-east-2:123456789012:task/test
[[ ! -s "$CREDENTIAL_REFRESH_COUNT_OUT" ]] ||
  fail "migration poll refreshed credentials before the 45-minute threshold"
AWS_CREDENTIALS_REFRESHED_AT=$((now - 45 * 60 - 1))
wait_task_ok staging arn:aws:ecs:us-east-2:123456789012:task/test
[[ "$(wc -l <"$CREDENTIAL_REFRESH_COUNT_OUT")" -eq 1 ]] ||
  fail "migration poll did not refresh credentials after 45 minutes"
now="$(date +%s)"
export CURL_FAIL=true
AWS_CREDENTIALS_REFRESHED_AT=$((now - 50 * 60))
warning="$(wait_task_ok staging arn:aws:ecs:us-east-2:123456789012:task/test 2>&1)" ||
  fail "long poll failed on a refresh error before 55 minutes"
[[ "$warning" == *"warning: AWS credential refresh failed before the 55-minute limit"* ]] ||
  fail "long poll did not warn after an early refresh failure"
AWS_CREDENTIALS_REFRESHED_AT=$((now - 56 * 60))
if wait_task_ok staging arn:aws:ecs:us-east-2:123456789012:task/test >/dev/null 2>&1; then
  fail "long poll accepted a refresh failure after 55 minutes"
fi
unset CURL_FAIL
unset ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN AWS_DEPLOY_ROLE_ARN
before_refresh_count="$(wc -l <"$CREDENTIAL_REFRESH_COUNT_OUT")"
wait_task_ok staging arn:aws:ecs:us-east-2:123456789012:task/test
[[ "$(wc -l <"$CREDENTIAL_REFRESH_COUNT_OUT")" -eq "$before_refresh_count" ]] ||
  fail "local migration poll refreshed credentials"

test_context="$repo_root/.image-key-test-$BASHPID"
mkdir -p "$test_context"
TEST_CONTEXT_PATH=".image-key-test-$BASHPID"
export TEST_CONTEXT_PATH
test_week="$(date -u +%G-W%V)"
trap 'rm -rf "$test_context"; rm -rf "$tmp"' EXIT
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine AS base
FROM base AS final
COPY --from=final /etc/os-release /os-release
DOCKERFILE
ignored_key="$(image_key "$test_context" ctx GIT_SHA=abc)"
[[ "$ignored_key" =~ ^ctx-[0-9a-f]{40}$ ]] || fail "valid context key: $ignored_key"
: >"$DIGEST_CALLS_OUT"
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$IMAGE_KEY_CTX" =~ ^ctx-[0-9a-f]{40}$ && "$IMAGE_KEY_TREE" =~ ^tree-[0-9a-f]{40}$ ]] ||
  fail "one image_inputs call did not set both keys"
((${#IMAGE_BUILD_CONTEXT_ARGS[@]} == 2)) || fail "pinned build-context args were not set"
[[ "$(wc -l <"$DIGEST_CALLS_OUT")" -eq 1 ]] || fail "FROM digest was resolved more than once"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM docker.io/node:20-alpine
DOCKERFILE
image_inputs "$test_context" "$test_week" GIT_SHA=abc
printf '%s\n' "${IMAGE_BUILD_CONTEXT_ARGS[@]}" |
  grep -Fxq 'node:20-alpine=docker-image://docker.io/node:20-alpine@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ||
  fail "normalized docker.io build-context alias was not pinned"
image_inputs "$test_context" "$test_week" --build-arg GIT_SHA=abc --build-arg FEATURE=on
staging_ctx="$IMAGE_KEY_CTX"
staging_tree="$IMAGE_KEY_TREE"
image_inputs "$test_context" "$test_week" GIT_SHA=abc FEATURE=off
[[ "$staging_tree" != "$IMAGE_KEY_TREE" ]] || fail "tree key ignored a non-GIT_SHA build arg"
image_inputs "$test_context" "$test_week" GIT_SHA=def FEATURE=on
preview_ctx="$IMAGE_KEY_CTX"
[[ "$staging_tree" == "$IMAGE_KEY_TREE" ]] || fail "tree key differs between staging and preview caller forms"
[[ "$staging_ctx" != "$preview_ctx" ]] || fail "ctx key ignored the staging GIT_SHA argument"
image_inputs "$test_context" "$test_week" GIT_SHA=abc FEATURE=on
[[ "$staging_ctx" == "$IMAGE_KEY_CTX" ]] || fail "normalized build arguments changed the ctx key"
image_inputs "$test_context" "$test_week"
[[ "$IMAGE_KEY_CTX" =~ ^ctx-[0-9a-f]{40}$ ]] || fail "empty build-arg array produced no key"
image_inputs "$test_context" 2026-W01 GIT_SHA=abc
week_one_key="$IMAGE_KEY_CTX"
image_inputs "$test_context" 2026-W02 GIT_SHA=abc
[[ "$week_one_key" != "$IMAGE_KEY_CTX" ]] || fail "week change kept the context key"
old_umask="$(umask)"
umask 022
image_inputs "$test_context" "$test_week" GIT_SHA=abc
umask_key="$IMAGE_KEY_CTX"
umask 077
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$umask_key" != "$IMAGE_KEY_CTX" ]] || fail "umask change kept the context key"
umask "$old_umask"
unset SOURCE_DATE_EPOCH BUILDX_METADATA_PROVENANCE
IMAGE_KEY_BUILD_FLAGS=(--platform linux/arm64 --provenance=false --file "$test_context/Dockerfile")
image_inputs "$test_context" "$test_week" GIT_SHA=abc
flag_key="$IMAGE_KEY_CTX"
IMAGE_KEY_BUILD_FLAGS+=(--target release)
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$flag_key" != "$IMAGE_KEY_CTX" ]] || fail "Buildx flags did not change the key"
IMAGE_KEY_BUILD_FLAGS=(--platform linux/arm64 --provenance=false --file "$test_context/Dockerfile")
image_inputs "$test_context" "$test_week" GIT_SHA=abc
epoch_key="$IMAGE_KEY_CTX"
export SOURCE_DATE_EPOCH=123
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$epoch_key" != "$IMAGE_KEY_CTX" ]] || fail "SOURCE_DATE_EPOCH did not change the key"
unset SOURCE_DATE_EPOCH
export BUILDX_METADATA_PROVENANCE=attest
image_inputs "$test_context" "$test_week" GIT_SHA=abc
env_key="$IMAGE_KEY_CTX"
export BUILDX_METADATA_PROVENANCE=min
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$env_key" != "$IMAGE_KEY_CTX" ]] || fail "BUILDX_* environment did not change the key"
unset BUILDX_METADATA_PROVENANCE
export BUILDX_GIT_LABELS=true BUILDX_GIT_INFO=true
image_inputs "$test_context" "$test_week" GIT_SHA=abc
env_key="$IMAGE_KEY_CTX"
export BUILDX_GIT_LABELS=false BUILDX_GIT_INFO=false
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$env_key" != "$IMAGE_KEY_CTX" ]] || fail "Buildx Git environment did not change the key"
unset BUILDX_GIT_LABELS BUILDX_GIT_INFO
export BUILDX_BUILDER=builder-one
image_inputs "$test_context" "$test_week" GIT_SHA=abc
builder_key="$IMAGE_KEY_CTX"
export BUILDX_BUILDER=builder-two
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$builder_key" == "$IMAGE_KEY_CTX" ]] || fail "builder identity changed the key"
unset BUILDX_BUILDER
image_build_flags "$test_context"
image_inputs "$test_context" "$test_week" GIT_SHA=abc
tree_path_one="$IMAGE_KEY_TREE"
mkdir -p "$tmp/another-checkout/backend"
cp "$test_context/Dockerfile" "$tmp/another-checkout/backend/Dockerfile"
image_build_flags "$tmp/another-checkout/backend"
image_inputs "$tmp/another-checkout/backend" "$test_week" GIT_SHA=abc
[[ "$tree_path_one" == "$IMAGE_KEY_TREE" ]] || fail "checkout path changed the tree key"
# shellcheck disable=SC2034
export BUILDKIT_DRIVER=docker
image_inputs "$test_context" "$test_week" GIT_SHA=abc
driver_key="$IMAGE_KEY_CTX"
# shellcheck disable=SC2034
export BUILDKIT_DRIVER=docker-container
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$driver_key" != "$IMAGE_KEY_CTX" ]] || fail "builder driver did not change the key"
unset BUILDKIT_DRIVER
IMAGE_KEY_BUILD_FLAGS=()
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
# escape=`
FROM node:20-alpine
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "escape directive produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
# check=skip=JSONArgsRecommended
FROM node:20-alpine
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "check directive produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine \
  AS continued
DOCKERFILE
[[ "$(image_key "$test_context" ctx GIT_SHA=abc)" =~ ^ctx-[0-9a-f]{40}$ ]] ||
  fail "valid continued FROM produced no key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
COPY --from=missing /etc/os-release /os-release
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "unknown COPY stage produced key"
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ -z "$IMAGE_KEY_CTX" && -z "$IMAGE_KEY_TREE" ]] || fail "failed key kept a partial image key"
((${#IMAGE_BUILD_CONTEXT_ARGS[@]} == 0)) || fail "failed key kept partial build contexts"
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
COPY \
  --from=ghcr.io/x /etc/os-release /os-release
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "continued COPY source produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
RUN \
  --mount=type=bind,from=ghcr.io/x,target=/source true
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "continued RUN source produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
RUN --mount=type=secret,id=token true
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "secret mount produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
RUN --mount=type=ssh true
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "ssh mount produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
RUN <<EOF
echo unsafe
EOF
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "RUN heredoc produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
COPY <<EOF /tmp/file
unsafe
EOF
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "COPY heredoc produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
RUN \
  --mount=type=bind,from=ghcr.io/x,target=/source true
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "continued RUN mount produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
ADD /etc/os-release /os-release \
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "continued ADD produced key"
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
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
COPY ["source", "/source"]
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "JSON COPY produced key"
cat >"$test_context/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
ADD ["source", "/source"]
DOCKERFILE
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "JSON ADD produced key"
FILTER_ATTR_PATH="$TEST_CONTEXT_PATH/Dockerfile"
FILTER_ATTR_NAME=filter
FILTER_ATTR_VALUE=lfs
export FILTER_ATTR_PATH FILTER_ATTR_NAME FILTER_ATTR_VALUE
printf '%s\n' 'FROM node:20-alpine AS pointer-one' >"$test_context/Dockerfile"
image_inputs "$test_context" "$test_week" GIT_SHA=abc
filtered_key_one="$IMAGE_KEY_CTX"
printf '%s\n' 'FROM node:20-alpine AS pointer-two' >"$test_context/Dockerfile"
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ "$filtered_key_one" != "$IMAGE_KEY_CTX" ]] || fail "filtered worktree bytes did not change the key"
ATTR_FAIL=true
export ATTR_FAIL
[[ -z "$(image_key "$test_context" ctx GIT_SHA=abc)" ]] || fail "check-attr failure produced key"
image_inputs "$test_context" "$test_week" GIT_SHA=abc
[[ -z "$IMAGE_KEY_CTX" && ${#IMAGE_BUILD_CONTEXT_ARGS[@]} -eq 0 ]] ||
  fail "check-attr failure kept image inputs"
printf '%s\n' 'image-key tests passed'
