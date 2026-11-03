#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../../../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/build" "$tmp/preview/infra/terraform/scripts"
cp "$repo_root/infra/terraform/scripts/"{build-push.sh,lib.sh,image-key.sh} "$tmp/build/"
cp "$repo_root/infra/terraform/scripts/"{preview.sh,lib.sh,image-key.sh} "$tmp/preview/infra/terraform/scripts/"
ln -s "$repo_root/backend" "$tmp/preview/backend"
ln -s "$repo_root/frontend" "$tmp/preview/frontend"
mkdir -p "$tmp/preview/deploy"
ln -s "$repo_root/deploy/litellm" "$tmp/preview/deploy/litellm"

cat >"$tmp/bin/aws" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$TEST_AWS_LOG"
case "$*" in
  *"ecr describe-repositories"*)
    printf '%s\n' '[["skynet/backend","123456789012.dkr.ecr.us-east-2.amazonaws.com/skynet/backend"],["skynet/frontend","123456789012.dkr.ecr.us-east-2.amazonaws.com/skynet/frontend"],["skynet/litellm","123456789012.dkr.ecr.us-east-2.amazonaws.com/skynet/litellm"]]'
    ;;
  *"ecr get-login-password"*) printf '%s\n' password ;;
  *"ecr describe-images"*)
    if [[ "$*" == *"--filter tagStatus=TAGGED"* ]]; then
      printf '%s\n' '{"imageDetails":[]}'
    elif [[ "$*" == *"imageTag=$TEST_SHA"* && "${TEST_EXISTING_BACKEND:-}" == true &&
      "$*" == *"repository-name skynet/backend"* ]]; then
      exit 0
    elif [[ "$*" == *"--query imageDetails[0].imagePushedAt"* &&
      "${TEST_CTX_HIT:-}" == true ]]; then
      date -u +%Y-%m-%dT%H:%M:%SZ
    else
      printf '%s\n' 'ImageNotFoundException' >&2
      exit 254
    fi
    ;;
  *"ecr batch-get-image"*)
    printf '%s\n' '{"images":[{"imageId":{"imageDigest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"imageManifest":"{\"schemaVersion\":2}","imageManifestMediaType":"application/vnd.oci.image.manifest.v1+json"}]}'
    ;;
  *"ecr put-image"*) ;;
  *"sts assume-role-with-web-identity"*)
    [[ "${TEST_FAIL_STS:-}" != true ]] || exit 1
    printf '%s\n' '{"Credentials":{"AccessKeyId":"test-access","SecretAccessKey":"test-secret","SessionToken":"test-session"}}'
    ;;
  *"sts get-caller-identity"*) printf '%s\n' 123456789012 ;;
  *"ecs "*) exit 99 ;;
  *) exit 2 ;;
esac
SH

cat >"$tmp/bin/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "buildx version") printf '%s\n' 'github.com/docker/buildx v0.36.1' ;;
  "buildx inspect --bootstrap") printf '%s\n' 'Driver: docker-container' 'BuildKit version: v0.32.2' ;;
  *"buildx imagetools inspect"*) printf '%s\n' '"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' ;;
  *"login"*) cat >/dev/null ;;
  *"buildx build"*)
    target=""
    metadata=""
    prior=""
    for arg in "$@"; do
      [[ "$prior" != -t ]] || target="${arg##*/}"
      [[ "$prior" != --metadata-file ]] || metadata="$arg"
      prior="$arg"
    done
    name="${target#skynet/}"
    name="${name%%:*}"
    printf 'build:%s\n' "$name" >>"$TEST_EVENT_LOG"
    [[ "${TEST_FAIL_BUILD:-}" != "$name" ]] || exit 17
    if [[ -n "$metadata" ]]; then
      printf '{"buildx.build.provenance":{"materials":[{"uri":"%s","digest":{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}]}}\n' \
        "${TEST_METADATA_URI:-pkg:docker/node?digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" \
        >"$metadata"
    fi
    ;;
  *) exit 2 ;;
esac
SH

cat >"$tmp/bin/git" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == -C ]]; then shift 2; fi
case "$1" in
  rev-parse)
    if [[ "$2" == --show-toplevel ]]; then
      printf '%s\n' "$TEST_REPO_ROOT"
    elif [[ "$2" == HEAD ]]; then
      printf '%s\n' "$TEST_SHA"
    else
      printf '%s\n' tree-one
    fi
    ;;
  ls-files)
    context="${*: -1}"
    printf '%s\0' "$context/Dockerfile"
    ;;
  check-attr)
    while IFS= read -r -d '' path; do
      for attr in filter eol text ident working-tree-encoding; do
        printf '%s\0%s\0unspecified\0' "$path" "$attr"
      done
    done
    ;;
  status) ;;
  *) exit 2 ;;
esac
SH

cat >"$tmp/bin/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'refresh\n' >>"$TEST_EVENT_LOG"
[[ "${TEST_FAIL_CURL:-}" != true ]] || exit 1
printf '%s\n' '{"value":"test-oidc-token"}'
SH
chmod +x "$tmp/bin/"{aws,docker,git,curl}
export PATH="$tmp/bin:$PATH" TEST_REPO_ROOT="$repo_root"
export TEST_SHA=8d68a0de4ea55e3a1666860d947a622d3273e3f1
export TEST_AWS_LOG="$tmp/aws.log" TEST_EVENT_LOG="$tmp/events.log"
export GITHUB_ACTIONS=true

cat >"$tmp/build/deploy.sh" <<'SH'
#!/usr/bin/env bash
printf 'migrate:%s\n' "${TEST_DEPLOY_STATUS:-unset}" >>"$TEST_EVENT_LOG"
exit "${TEST_DEPLOY_STATUS:-0}"
SH
chmod +x "$tmp/build/deploy.sh"

CURRENT_LOG=""

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  [[ -z "$CURRENT_LOG" || ! -f "$CURRENT_LOG" ]] || tail -n 80 "$CURRENT_LOG" >&2
  [[ ! -f "$TEST_AWS_LOG" ]] || tail -n 40 "$TEST_AWS_LOG" >&2
  [[ ! -f "$TEST_EVENT_LOG" ]] || tail -n 40 "$TEST_EVENT_LOG" >&2
  exit 1
}

run_build() {
  local output="$1" log="$2"
  : >"$TEST_AWS_LOG"
  : >"$TEST_EVENT_LOG"
  : >"$output"
  CURRENT_LOG="$log"
  : >"$CURRENT_LOG"
  if "$tmp/build/build-push.sh" staging "$TEST_SHA" --migrate >"$CURRENT_LOG" 2>&1; then
    fail "build-push succeeded in failure test: $output"
  fi
  ! rg -q '^migrated=' "$output" || fail "failed lane wrote migrated output: $output"
}

export GITHUB_OUTPUT="$tmp/migrated"
export TEST_DEPLOY_STATUS=1
run_build "$GITHUB_OUTPUT" "$tmp/migration.log"
rg -q '^migrate:1$' "$TEST_EVENT_LOG" || fail "migration stub did not fail the deploy lane"

export TEST_DEPLOY_STATUS=0 TEST_FAIL_CURL=true
export ACTIONS_ID_TOKEN_REQUEST_URL=https://token.invalid/request
export ACTIONS_ID_TOKEN_REQUEST_TOKEN=test-request-token
export AWS_DEPLOY_ROLE_ARN=arn:aws:iam::123456789012:role/test
run_build "$GITHUB_OUTPUT" "$tmp/credentials.log"
if rg -q '^build:' "$TEST_EVENT_LOG"; then fail "build ran after credential refresh failed"; fi

unset TEST_FAIL_CURL ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN AWS_DEPLOY_ROLE_ARN
export TEST_FAIL_BUILD=backend
run_build "$GITHUB_OUTPUT" "$tmp/build.log"

unset TEST_FAIL_BUILD
export TEST_EXISTING_BACKEND=true
: >"$TEST_AWS_LOG"
: >"$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/skip.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" staging "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "existing immutable backend tag did not skip"
if rg -q 'put-image.*--image-tag tree-' "$TEST_AWS_LOG"; then
  fail "existing SHA tag received an unverified tree-key tag"
fi

unset TEST_EXISTING_BACKEND
export TEST_CTX_HIT=true
: >"$TEST_AWS_LOG"
: >"$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/reuse.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" staging "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "fresh context-key reuse failed"
if rg -q 'put-image.*--image-tag tree-' "$TEST_AWS_LOG"; then
  fail "context-key reuse added a new tree-key tag"
fi

unset TEST_CTX_HIT
rm -f "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/production.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" production "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "production build failed"
if rg -q 'describe-images.*repository-name skynet/(backend|frontend|litellm)' "$TEST_AWS_LOG"; then
  fail "production looked up a shared application image tag"
fi
if rg -q 'batch-get-image.*repository-name skynet/(backend|frontend|litellm)' "$TEST_AWS_LOG"; then
  fail "production retagged a shared application image"
fi
if rg -q 'put-image.*--image-tag (ctx|tree)-' "$TEST_AWS_LOG"; then
  fail "production added a shared image-key tag"
fi
[[ "$(rg -c '^build:' "$TEST_EVENT_LOG")" -eq 3 ]] ||
  fail "production did not build all three application images"

export TEST_METADATA_URI=unknown:material
: >"$TEST_AWS_LOG"
: >"$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/unverified-provenance.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" staging "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "staging build failed when source metadata was unknown"
if rg -q 'put-image.*--image-tag (ctx|tree)-' "$TEST_AWS_LOG"; then
  fail "unverified source metadata received an image-key tag"
fi
unset TEST_METADATA_URI

rm -f "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/preview-backend.log"
: >"$CURRENT_LOG"
# The source path points at a test copy in the temporary fixture.
# shellcheck disable=SC1091
source "$tmp/preview/infra/terraform/scripts/preview.sh" build 123 "$TEST_SHA"
pr=123
sha="$TEST_SHA"
export ACTIONS_ID_TOKEN_REQUEST_URL=https://token.invalid/request
export ACTIONS_ID_TOKEN_REQUEST_TOKEN=test-request-token
export AWS_DEPLOY_ROLE_ARN=arn:aws:iam::123456789012:role/test
export TEST_FAIL_BUILD=preview-backend
cfg='{"domain":"staging.skynetml.com"}'
if run_preview_lane preview-backend backend_and_migrate \
  123456789012.dkr.ecr.us-east-2.amazonaws.com/skynet/preview-backend:"$sha-pr-$pr" "$cfg" \
  >"$CURRENT_LOG" 2>&1; then
  fail "failed preview backend build returned success"
fi
rg -q '^refresh$' "$TEST_EVENT_LOG" || fail "preview build did not use the successful credential stub"
if rg -q 'ecs ' "$TEST_AWS_LOG"; then fail "preview migration ran after its backend build failed"; fi

unset TEST_FAIL_BUILD
export TEST_FAIL_BUILD=preview-frontend
rm -f "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/preview-frontend.log"
: >"$CURRENT_LOG"
if run_preview_lane preview-frontend build_frontend \
  123456789012.dkr.ecr.us-east-2.amazonaws.com/skynet/preview-frontend "$sha-pr-$pr" staging.skynetml.com \
  >"$CURRENT_LOG" 2>&1; then
  fail "failed preview frontend build returned success"
fi

printf '%s\n' 'lane failure stub tests passed'
