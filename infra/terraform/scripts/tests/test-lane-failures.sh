#!/usr/bin/env bash
set -euo pipefail

for command in awk find grep git jq sed timeout; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'missing test command: %s\n' "$command" >&2
    exit 1
  }
done

repo_root="$(cd "$(dirname "$0")/../../../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/build" "$tmp/preview/infra/terraform/scripts"
real_rm="$(command -v rm)"
real_find="$(command -v find)"
real_sed="$(command -v sed)"
real_stat="$(command -v stat)"
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
      if [[ "${TEST_CTX_HIT:-}" == true ]]; then
        printf '{"imageDetails":[{"imageDigest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","imagePushedAt":"%s","imageTags":["ctx-test"]}]}\n' \
          "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      else
        printf '%s\n' '{"imageDetails":[]}'
      fi
    elif [[ "$*" == *"imageTag=$TEST_SHA"* && "${TEST_EXISTING_BACKEND:-}" == true &&
      "$*" == *"repository-name skynet/backend"* ]]; then
      exit 0
    elif [[ "${TEST_EXISTING_TAGS:-}" == true &&
      "$*" == *"repository-name skynet/backend"* && "$*" == *"imageTag=$TEST_SHA"* ]]; then
      exit 0
    elif [[ "${TEST_EXISTING_TAGS:-}" == true &&
      "$*" == *"repository-name skynet/frontend"* && "$*" == *"imageTag=$TEST_SHA-production"* ]]; then
      exit 0
    elif [[ "${TEST_EXISTING_TAGS:-}" == true &&
      "$*" == *"repository-name skynet/litellm"* && "$*" == *"imageTag=$TEST_SHA"* ]]; then
      exit 0
    elif [[ "${TEST_EXISTING_NON_BACKEND:-}" == true &&
      "$*" == *"repository-name skynet/frontend"* && "$*" == *"imageTag=$TEST_SHA-staging"* ]]; then
      exit 0
    elif [[ "${TEST_EXISTING_NON_BACKEND:-}" == true &&
      "$*" == *"repository-name skynet/litellm"* && "$*" == *"imageTag=$TEST_SHA"* ]]; then
      exit 0
    elif [[ "$*" == *"repository-name skynet/backend"* && "$*" == *"imageTag=$TEST_SHA"* &&
      "${TEST_PUSH_RACE:-}" == true && -e "$TEST_RACE_TAG_FLAG" ]]; then
      exit 0
    elif [[ "$*" == *"--query imageDetails[0].imagePushedAt"* &&
      "${TEST_CTX_HIT:-}" == true ]]; then
      date -u +%Y-%m-%dT%H:%M:%SZ
    elif [[ "$*" == *"--query imageDetails[0].imageDigest"* &&
      "${TEST_CTX_HIT:-}" == true ]]; then
      printf '%s\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
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
  *"buildx imagetools inspect"*)
    if [[ "$*" == *" --raw "* ]]; then
      cat "$TEST_MULTIARCH_INDEX_FILE"
    else
      printf '%s\n' '"sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"'
    fi
    ;;
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
    if [[ "${TEST_FAIL_BUILD:-}" == "$name" ]]; then
      [[ "${TEST_PUSH_RACE:-}" != true ]] || : >"$TEST_RACE_TAG_FLAG"
      exit 17
    fi
    if [[ "${TEST_KILL_LANE:-}" == "$name" ]]; then
      lane_pid="$(ps -o ppid= -p "$PPID" | tr -d '[:space:]')"
      [[ "$lane_pid" =~ ^[0-9]+$ ]] || exit 18
      kill -KILL "$lane_pid"
    fi
    if [[ -n "$metadata" ]]; then
      if [[ "${TEST_NO_MATERIALS:-}" == true ]]; then
        printf '%s\n' '{"buildx.build.provenance":{"materials":[]}}' >"$metadata"
      else
        printf '{"buildx.build.provenance":{"materials":[{"uri":"%s","digest":{"sha256":"b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"}}]}}\n' \
          "${TEST_METADATA_URI:-pkg:docker/node?digest=sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30}" \
          >"$metadata"
      fi
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
    if [[ "$*" == *" --stage "* ]]; then
      printf '100644 0123456789abcdef0123456789abcdef01234567 0\t%s/Dockerfile\0' "$context"
      exit 0
    fi
    if [[ "$*" == *" -v "* ]]; then
      printf 'H %s/Dockerfile\n' "$context"
      exit 0
    fi
    printf '%s\0' "$context/Dockerfile"
    ;;
  config) exit 1 ;;
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
if [[ "${TEST_FAIL_CURL_AFTER_BACKEND:-}" == true ]] &&
  grep -Fxq 'build:backend' "$TEST_EVENT_LOG"; then
  exit 1
fi
printf '%s\n' '{"value":"test-oidc-token"}'
SH
chmod +x "$tmp/bin/"{aws,docker,git,curl}
cat >"$tmp/bin/sed" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${TEST_FAIL_LOGGER:-}" == true ]]; then
  cat >/dev/null
  exit 1
fi
exec "$TEST_REAL_SED" "$@"
SH
cat >"$tmp/bin/rm" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${TEST_PRESERVE_LANES:-}" == true && "${1:-}" == -rf &&
  -n "${TEST_LANE_TMP:-}" && "${2:-}" == "$TEST_LANE_TMP/"* ]]; then
  exit 0
fi
exec "$TEST_REAL_RM" "$@"
SH
chmod +x "$tmp/bin/"{sed,rm}
cat >"$tmp/bin/find" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
  [[ "$arg" != -mindepth ]] || exec "$TEST_REAL_FIND" "$@"
done
root="$1"
printf '%s\0%s/Dockerfile\0' "$root" "$root"
SH
cat >"$tmp/bin/stat" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
target="${@: -1}"
if [[ -d "$target" ]]; then
  printf '755\n'
elif [[ -e "$target" ]]; then
  printf '644\n'
else
  exec "$TEST_REAL_STAT" "$@"
fi
SH
chmod +x "$tmp/bin/"{find,stat}
export PATH="$tmp/bin:$PATH" TEST_REPO_ROOT="$repo_root"
export TEST_MULTIARCH_INDEX_FILE="$repo_root/infra/terraform/scripts/tests/python-3.12-slim-index.json"
export TEST_REAL_FIND="$real_find"
export TEST_REAL_STAT="$real_stat"
export TEST_SHA=8d68a0de4ea55e3a1666860d947a622d3273e3f1
export TEST_AWS_LOG="$tmp/aws.log" TEST_EVENT_LOG="$tmp/events.log"
export TEST_RACE_TAG_FLAG="$tmp/pushed-by-race"
export TEST_REAL_RM="$real_rm" TEST_REAL_SED="$real_sed"
export GITHUB_ACTIONS=true

cat >"$tmp/build/deploy.sh" <<'SH'
#!/usr/bin/env bash
printf 'migrate:%s\n' "${TEST_DEPLOY_STATUS:-unset}" >>"$TEST_EVENT_LOG"
exit "${TEST_DEPLOY_STATUS:-0}"
SH
chmod +x "$tmp/build/deploy.sh"

CURRENT_LOG=""

prepare_lane_tmp() {
  TEST_LANE_TMP="$tmp/lane-$1"
  mkdir -p "$TEST_LANE_TMP"
  export TEST_LANE_TMP TMPDIR="$TEST_LANE_TMP" TEST_PRESERVE_LANES=true
}

lane_dir_for_test() {
  find "$TEST_LANE_TMP" -mindepth 1 -maxdepth 1 -type d -print -quit
}

assert_lane_status() {
  local lane="$1" result="$2"
  [[ -n "${TEST_LAST_LANE_DIR:-}" && -e "$TEST_LAST_LANE_DIR/$lane.$result" ]] ||
    fail "$lane lane did not publish .$result"
}

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  [[ -z "$CURRENT_LOG" || ! -f "$CURRENT_LOG" ]] || tail -n 80 "$CURRENT_LOG" >&2
  [[ ! -f "$TEST_AWS_LOG" ]] || tail -n 40 "$TEST_AWS_LOG" >&2
  [[ ! -f "$TEST_EVENT_LOG" ]] || tail -n 40 "$TEST_EVENT_LOG" >&2
  exit 1
}

assert_no_match() {
  local status
  if grep -Eq -- "$1" "$2"; then
    return 1
  else
    status=$?
  fi
  [[ "$status" -eq 1 ]]
}

run_build() {
  local output="$1" log="$2" lane_case="$3"
  prepare_lane_tmp "$lane_case"
  : >"$TEST_AWS_LOG"
  : >"$TEST_EVENT_LOG"
  : >"$output"
  CURRENT_LOG="$log"
  : >"$CURRENT_LOG"
  if "$tmp/build/build-push.sh" staging "$TEST_SHA" --migrate >"$CURRENT_LOG" 2>&1; then
    fail "build-push succeeded in failure test: $output"
  fi
  assert_no_match '^migrated=' "$output" || fail "failed lane wrote migrated output: $output"
  TEST_LAST_LANE_DIR="$(lane_dir_for_test)"
}

export GITHUB_OUTPUT="$tmp/migrated"
export TEST_DEPLOY_STATUS=1
run_build "$GITHUB_OUTPUT" "$tmp/migration.log" migration
grep -Eq '^migrate:1$' "$TEST_EVENT_LOG" || fail "migration stub did not fail the deploy lane"

export TEST_DEPLOY_STATUS=0 TEST_FAIL_CURL=true
export ACTIONS_ID_TOKEN_REQUEST_URL=https://token.invalid/request
export ACTIONS_ID_TOKEN_REQUEST_TOKEN=test-request-token
export AWS_DEPLOY_ROLE_ARN=arn:aws:iam::123456789012:role/test
run_build "$GITHUB_OUTPUT" "$tmp/credentials.log" credentials
assert_no_match '^build:' "$TEST_EVENT_LOG" || fail "build ran after credential refresh failed"

unset TEST_FAIL_CURL ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN AWS_DEPLOY_ROLE_ARN
export TEST_FAIL_BUILD=backend
run_build "$GITHUB_OUTPUT" "$tmp/build.log" build
assert_lane_status backend failed
assert_no_match '^migrate:' "$TEST_EVENT_LOG" ||
  fail "backend lane migrated after die terminated its build"

unset TEST_FAIL_BUILD
export TEST_FAIL_LOGGER=true
run_build "$GITHUB_OUTPUT" "$tmp/logger.log" logger
assert_lane_status litellm failed
grep -Fxq 'build:backend' "$TEST_EVENT_LOG" ||
  fail "logger failure test did not run the backend command"
assert_no_match '^migrate:' "$TEST_EVENT_LOG" ||
  fail "migration ran after the logger failed"
unset TEST_FAIL_LOGGER

prepare_lane_tmp dead-process
: >"$TEST_AWS_LOG"
: >"$TEST_EVENT_LOG"
: >"$GITHUB_OUTPUT"
CURRENT_LOG="$tmp/dead-process.log"
: >"$CURRENT_LOG"
export TEST_KILL_LANE=litellm
if timeout 10 "$tmp/build/build-push.sh" staging "$TEST_SHA" --migrate \
  >"$CURRENT_LOG" 2>&1; then
  fail "dead lane process returned success"
else
  status=$?
  [[ "$status" -ne 124 ]] || fail "dead lane process made wait_for_lane_success hang"
fi
TEST_LAST_LANE_DIR="$(lane_dir_for_test)"
[[ -n "$TEST_LAST_LANE_DIR" ]] || fail "dead lane test did not preserve its lane directory"
[[ ! -e "$TEST_LAST_LANE_DIR/litellm.ok" && ! -e "$TEST_LAST_LANE_DIR/litellm.failed" ]] ||
  fail "dead lane process published a status marker"
assert_no_match '^migrate:' "$TEST_EVENT_LOG" ||
  fail "migration ran after a sibling lane process disappeared"
unset TEST_KILL_LANE

export TEST_EXISTING_BACKEND=true
: >"$TEST_AWS_LOG"
: >"$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/skip.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" staging "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "existing immutable backend tag did not skip"
assert_no_match 'put-image.*--image-tag tree-' "$TEST_AWS_LOG" ||
  fail "existing SHA tag received an unverified tree-key tag"

unset TEST_EXISTING_BACKEND
export TEST_CTX_HIT=true
: >"$TEST_AWS_LOG"
: >"$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/reuse.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" staging "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "fresh context-key reuse failed"
grep -Fxq 'build:backend' "$TEST_EVENT_LOG" ||
  fail "backend shared tag reused a fresh context key"
grep -Fxq 'build:litellm' "$TEST_EVENT_LOG" ||
  fail "staging litellm reused a fresh context key"
assert_no_match '^build:frontend$' "$TEST_EVENT_LOG" ||
  fail "staging frontend did not reuse its fresh context key"
grep -Eq "put-image --repository-name skynet/frontend --image-tag $TEST_SHA-staging" "$TEST_AWS_LOG" ||
  fail "staging frontend context-key hit did not retag its environment-scoped tag"
assert_no_match 'put-image.*repository-name skynet/(backend|litellm).*--image-tag ctx-' "$TEST_AWS_LOG" ||
  fail "shared backend or LiteLLM tag received an unused context-key tag"

unset TEST_CTX_HIT
rm -f "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/production.log"
: >"$CURRENT_LOG"
export TEST_EXISTING_TAGS=true
: >"$TEST_EVENT_LOG"
"$tmp/build/build-push.sh" production "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "production skipped existing immutable tags"
assert_no_match '^build:' "$TEST_EVENT_LOG" || fail "production rebuilt an existing immutable tag"
assert_no_match '--query imageDetails\[0\]\.imagePushedAt' "$TEST_AWS_LOG" ||
  fail "production called the keyed-image freshness lookup"
assert_no_match 'batch-get-image.*repository-name skynet/(backend|frontend|litellm)' "$TEST_AWS_LOG" ||
  fail "production retagged a shared application image"
assert_no_match 'put-image.*--image-tag (ctx|tree)-' "$TEST_AWS_LOG" ||
  fail "production added a shared image-key tag"
unset TEST_EXISTING_TAGS

export TEST_FAIL_BUILD=backend TEST_PUSH_RACE=true
rm -f "$TEST_RACE_TAG_FLAG" "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/production-push-race.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" production "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "production did not accept the immutable-tag race fallback"
assert_no_match '--query imageDetails\[0\]\.imagePushedAt' "$TEST_AWS_LOG" ||
  fail "production called the keyed-image freshness lookup"
assert_no_match 'batch-get-image.*repository-name skynet/(backend|frontend|litellm)' "$TEST_AWS_LOG" ||
  fail "production retagged a shared application image"
assert_no_match 'put-image.*--image-tag (ctx|tree)-' "$TEST_AWS_LOG" ||
  fail "production wrote a shared image-key tag"
unset TEST_FAIL_BUILD TEST_PUSH_RACE

rm -f "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/production-build.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" production "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "production build failed without an existing tag"
[[ "$(grep -c '^build:' "$TEST_EVENT_LOG")" -eq 3 ]] ||
  fail "production did not build all three application images"
assert_no_match '--query imageDetails\[0\]\.imagePushedAt' "$TEST_AWS_LOG" ||
  fail "production called the keyed-image freshness lookup"
assert_no_match 'batch-get-image.*repository-name skynet/(backend|frontend|litellm)' "$TEST_AWS_LOG" ||
  fail "production retagged a shared application image"
assert_no_match 'put-image.*--image-tag (ctx|tree)-' "$TEST_AWS_LOG" ||
  fail "production wrote a shared image-key tag"

export TEST_METADATA_URI=unknown:material
: >"$TEST_AWS_LOG"
: >"$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/unverified-provenance.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" staging "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "staging build failed when source metadata was unknown"
assert_no_match 'put-image.*--image-tag (ctx|tree)-' "$TEST_AWS_LOG" ||
  fail "unverified source metadata received an image-key tag"
grep -Eq 'warning: no reusable key tag written: provenance sources did not verify' "$CURRENT_LOG" ||
  fail "unverified provenance did not explain why no key tag was written"
unset TEST_METADATA_URI

export TEST_NO_MATERIALS=true
rm -f "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/no-provenance-materials.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" staging "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "staging build failed with empty provenance materials"
grep -Eq 'warning: no reusable key tag written: provenance materials missing' "$CURRENT_LOG" ||
  fail "empty provenance materials did not explain why no key tag was written"
unset TEST_NO_MATERIALS

export TEST_EXISTING_NON_BACKEND=true TEST_FAIL_CURL_AFTER_BACKEND=true
export ACTIONS_ID_TOKEN_REQUEST_URL=https://token.invalid/request
export ACTIONS_ID_TOKEN_REQUEST_TOKEN=test-request-token
export AWS_DEPLOY_ROLE_ARN=arn:aws:iam::123456789012:role/test
rm -f "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/post-push-refresh.log"
: >"$CURRENT_LOG"
"$tmp/build/build-push.sh" staging "$TEST_SHA" >"$CURRENT_LOG" 2>&1 ||
  fail "post-push credential refresh failure made a successful build fail"
grep -Fxq 'build:backend' "$TEST_EVENT_LOG" || fail "post-push refresh test did not build backend"
assert_no_match '^build:(frontend|litellm)$' "$TEST_EVENT_LOG" ||
  fail "post-push refresh test unexpectedly built an existing image"
grep -Eq 'warning: cannot refresh AWS credentials after push; skipping image-key tags' "$CURRENT_LOG" ||
  fail "post-push credential refresh failure did not warn and skip key tags"
assert_no_match 'put-image.*--image-tag (ctx|tree)-' "$TEST_AWS_LOG" ||
  fail "post-push credential refresh failure still wrote a key tag"
unset TEST_EXISTING_NON_BACKEND TEST_FAIL_CURL_AFTER_BACKEND
unset ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN AWS_DEPLOY_ROLE_ARN

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
grep -Eq '^refresh$' "$TEST_EVENT_LOG" || fail "preview build did not use the successful credential stub"
assert_no_match 'ecs ' "$TEST_AWS_LOG" ||
  fail "preview migration ran after its backend build failed"

slot_of() { printf 'slot-1\n'; }
preview_env() { printf '{}\n'; }
account() { printf '123456789012\n'; }
register() { echo "register stub failed" >&2; return 17; }
export BACKEND_IMAGE=123456789012.dkr.ecr.us-east-2.amazonaws.com/skynet/backend:staging
rm -f "$TEST_AWS_LOG" "$TEST_EVENT_LOG"
CURRENT_LOG="$tmp/preview-register.log"
: >"$CURRENT_LOG"
if run_preview_lane preview-backend backend_and_migrate "$BACKEND_IMAGE" "$cfg" \
  >"$CURRENT_LOG" 2>&1; then
  fail "preview backend lane accepted a failed register step"
fi
assert_no_match 'migration ok' "$CURRENT_LOG" ||
  fail "preview lane reported migration ok after register failed"
assert_no_match 'ecs (run-task|update-service|create-service)' "$TEST_AWS_LOG" ||
  fail "preview lane rolled services after register failed"
unset BACKEND_IMAGE

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

preview_lookup_step="$tmp/preview-backend-lookup.sh"
awk '
  $0 == "      - name: Find a reusable backend image" { in_step=1; next }
  in_step && /^      - / { exit }
  in_step && /^        run: \|$/ { in_run=1; next }
  in_run && /^          / { print substr($0, 11); next }
  in_run { exit }
' "$TEST_REPO_ROOT/.github/workflows/preview.yml" >"$preview_lookup_step"
[[ -s "$preview_lookup_step" ]] || fail "preview backend lookup workflow step was not found"
cat >"$tmp/preview/infra/terraform/scripts/preview.sh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' 'simulated backend image lookup failure' >&2
exit 17
SH
chmod +x "$tmp/preview/infra/terraform/scripts/preview.sh"
preview_lookup_output="$tmp/preview-backend-image.txt"
: >"$preview_lookup_output"
CURRENT_LOG="$tmp/preview-backend-lookup.log"
if ! (
  cd "$tmp/preview"
  export PR=123 SHA="$TEST_SHA" GITHUB_OUTPUT="$preview_lookup_output"
  bash -e -o pipefail "$preview_lookup_step"
) >"$CURRENT_LOG" 2>&1; then
  fail "backend image lookup error failed the preview step"
fi
grep -Fxq 'image=' "$preview_lookup_output" ||
  fail "backend image lookup error did not select a full backend build"
grep -Fq '::warning::backend image lookup failed; building a new backend image' "$CURRENT_LOG" ||
  fail "backend image lookup error did not emit a warning"

printf '%s\n' 'lane failure stub tests passed'
