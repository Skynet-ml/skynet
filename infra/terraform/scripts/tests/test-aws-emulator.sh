#!/usr/bin/env bash
# Runs the deploy script helpers against a local AWS Emulator with the real
# aws CLI (SKY-708). It covers deploy_config, the ECR helpers of
# image-key.sh and the commands of secrets.sh. Read docs/agents/aws-emulator.md.
#
# Start the emulator first: scripts/aws-emulator.sh start
# With no emulator, the test prints SKIP and exits 0.
# AWS_EMULATOR_REQUIRED=1 makes that a failure.
set -euo pipefail

port="${AWS_EMULATOR_PORT:-9297}"
endpoint="https://127.0.0.1:$port"
files="$HOME/.aws/emulator/$port"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

skip() {
  [[ "${AWS_EMULATOR_REQUIRED:-}" != 1 ]] || fail "AWS_EMULATOR_REQUIRED=1, but $*"
  printf 'SKIP: %s\n' "$*"
  exit 0
}

for command in aws curl jq python3; do
  command -v "$command" >/dev/null 2>&1 || skip "missing test command: $command"
done
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 ||
  skip "missing test command: sha256sum or shasum"
[[ -f "$files/emulator_credentials" && -f "$files/config" && -f "$files/ca.pem" ]] ||
  skip "no AWS Emulator files in $files; run scripts/aws-emulator.sh start"
[[ "$(curl -fsS --max-time 2 --cacert "$files/ca.pem" "$endpoint/__admin/health" 2>/dev/null)" == ok ]] ||
  skip "no AWS Emulator answers on $endpoint"

# Remove every way to reach a real account, also the GitHub Actions OIDC path
# of lib.sh, before the scripts load.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_DEFAULT_PROFILE \
  AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_ARN AWS_ROLE_SESSION_NAME \
  AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_CREDENTIALS_FULL_URI \
  AWS_CONTAINER_AUTHORIZATION_TOKEN \
  GITHUB_ACTIONS ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN AWS_DEPLOY_ROLE_ARN
export AWS_ENDPOINT_URL="$endpoint"
export AWS_SHARED_CREDENTIALS_FILE="$files/emulator_credentials"
export AWS_CONFIG_FILE="$files/config"
# The config file names a CA path inside the container; this one is on the host.
export AWS_CA_BUNDLE="$files/ca.pem"
export AWS_PROFILE=default
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_EC2_METADATA_DISABLED=true
export AWS_PAGER=""

# Stops the test unless every aws call goes to the emulator account. Without
# it, secrets.sh would write skynet/staging/* secrets on real AWS.
guard() {
  local account
  [[ "$AWS_ENDPOINT_URL" == "https://127.0.0.1:$port" ]] ||
    fail "refused: the AWS endpoint is $AWS_ENDPOINT_URL, not https://127.0.0.1:$port"
  [[ "$AWS_PROFILE" == default && "$AWS_REGION" == us-east-1 ]] ||
    fail "refused: the AWS profile or region is not the emulator's"
  account="$(aws sts get-caller-identity --query Account --output text)" ||
    fail "refused: sts get-caller-identity failed"
  [[ "$account" == 000000000000 ]] ||
    fail "refused: STS returned account $account, not the emulator account 000000000000"
}
guard

repo_root="$(cd "$(dirname "$0")/../../../.." && pwd)"
scripts="$repo_root/infra/terraform/scripts"
# shellcheck source=infra/terraform/scripts/image-key.sh
source "$scripts/image-key.sh"
# shellcheck source=infra/terraform/scripts/lib.sh
source "$scripts/lib.sh"
# The scripts set their own defaults, so check again before the first write.
guard

run_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
repo="skynet-test/emulator-$run_id"
tmp="$(mktemp -d)"
cleanup() {
  aws ecr delete-repository --repository-name "$repo" --force >/dev/null 2>&1 || true
  aws ssm delete-parameter --name "/skynet/emulator-$run_id/deploy" >/dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# --- deploy_config --------------------------------------------------------
aws ssm put-parameter --name "/skynet/emulator-$run_id/deploy" --type String \
  --value '{"cluster":"skynet-test"}' >/dev/null
[[ "$(deploy_config "emulator-$run_id")" == '{"cluster":"skynet-test"}' ]] ||
  fail "deploy_config did not return the stored value"
printf 'ok: deploy_config\n'

# --- image-key.sh ECR helpers ----------------------------------------------
# Pushes one image with no layers. A unique config blob gives each image its
# own digest, and no Docker is needed.
push_image() {
  local tag="$1" config="$tmp/$1.json" digest upload size
  printf '{"architecture":"amd64","os":"linux","config":{"Labels":{"tag":"%s"}},"rootfs":{"type":"layers","diff_ids":[]}}' \
    "$tag" >"$config"
  digest="sha256:$(sha256 "$config")"
  size="$(wc -c <"$config" | tr -d ' ')"
  upload="$(aws ecr initiate-layer-upload --repository-name "$repo" --query uploadId --output text)"
  aws ecr upload-layer-part --repository-name "$repo" --upload-id "$upload" \
    --part-first-byte 0 --part-last-byte "$((size - 1))" --layer-part-blob "fileb://$config" >/dev/null
  aws ecr complete-layer-upload --repository-name "$repo" --upload-id "$upload" --layer-digests "$digest" >/dev/null
  aws ecr put-image --repository-name "$repo" --image-tag "$tag" --image-manifest \
    "{\"schemaVersion\":2,\"mediaType\":\"application/vnd.docker.distribution.manifest.v2+json\",\"config\":{\"mediaType\":\"application/vnd.docker.container.image.v1+json\",\"size\":$size,\"digest\":\"$digest\"},\"layers\":[]}" \
    >/dev/null
}

digest_of() {
  aws ecr describe-images --repository-name "$repo" --image-ids "imageTag=$1" \
    --query 'imageDetails[0].imageDigest' --output text
}

aws ecr create-repository --repository-name "$repo" >/dev/null
push_image cache-old
# imagePushedAt must differ, so latest_cache_ref has one newest tag.
sleep 1.1
push_image cache-new

find_by_key "$repo" cache-old || fail "find_by_key did not find an existing tag"
! find_by_key "$repo" no-such-tag || fail "find_by_key found a missing tag"
find_fresh_by_key "$repo" cache-new || fail "find_fresh_by_key refused a fresh tag"
! find_fresh_by_key "$repo" no-such-tag 2>/dev/null || fail "find_fresh_by_key accepted a missing tag"
printf 'ok: find_by_key, find_fresh_by_key\n'

retag "$repo" cache-new key-1 || fail "the first retag failed"
[[ "$(digest_of key-1)" == "$(digest_of cache-new)" ]] || fail "the retag points at another digest"
# The emulator answers ImageAlreadyExistsException here, as ECR does.
retag "$repo" cache-new key-1 || fail "a repeat retag of the same digest failed"
! retag "$repo" no-such-tag key-2 2>/dev/null || fail "retag of a missing tag succeeded"
printf 'ok: retag, also the repeat\n'

[[ "$(latest_cache_ref "$repo" cache-)" == cache-new ]] ||
  fail "latest_cache_ref did not return the newest cache tag"
[[ -z "$(latest_cache_ref "$repo" none-)" ]] || fail "latest_cache_ref returned a tag for an unknown prefix"
printf 'ok: latest_cache_ref\n'

# --- secrets.sh --------------------------------------------------------------
# secrets.sh accepts only staging or production, so this part resets the
# fixed staging names in the emulator on each run.
env=staging
secrets_sh() {
  bash "$scripts/secrets.sh" "$@"
}

# Deletes the secret, then creates it with no value and prints its ARN. The
# emulator ends a forced delete a short time after it answers, so the create
# tries again for at most 10 seconds.
reset_secret() {
  local id="skynet/$env/$1" i
  aws secretsmanager delete-secret --secret-id "$id" --force-delete-without-recovery >/dev/null 2>&1 || true
  for ((i = 0; i < 20; i++)); do
    aws secretsmanager create-secret --name "$id" --query ARN --output text 2>/dev/null && return 0
    sleep 0.5
  done
  fail "cannot create $id again after its delete"
}

arns=()
for name in app frontend litellm; do
  # No value: the set command must start from {} for a secret with no version.
  arns+=("$(reset_secret "$name")")
done

register() {
  local family="$1" secrets="$2"
  aws ecs register-task-definition --family "skynet-$env-$family" --cli-input-json \
    "{\"executionRoleArn\":\"arn:aws:iam::000000000000:role/skynet-test-execution\",\"containerDefinitions\":[{\"name\":\"$family\",\"image\":\"busybox\",\"memory\":512,\"secrets\":$secrets}]}" \
    >/dev/null
}
from() {
  jq -nc --arg arn "$1" '$ARGS.positional | map({name: ., valueFrom: "\($arn):\(.)::"})' --args "${@:2}"
}
register backend "$(from "${arns[0]}" DATABASE_URL SESSION_KEY)"
register worker "$(from "${arns[0]}" DATABASE_URL WORKER_TOKEN)"
register migrate "$(from "${arns[0]}" DATABASE_URL)"
register frontend "$(from "${arns[1]}" NEXT_SECRET)"
register litellm "$(from "${arns[2]}" LITELLM_MASTER_KEY)"

value_from="$(aws ecs describe-task-definition --task-definition "skynet-$env-backend" \
  --query 'taskDefinition.containerDefinitions[0].secrets[0].valueFrom' --output text)"
[[ "$value_from" == "${arns[0]}:DATABASE_URL::" ]] ||
  fail "the emulator does not return secrets[].valueFrom; secrets.sh cannot read its keys"

[[ "$(secrets_sh template "$env" app)" == "$(jq -n '{DATABASE_URL: "", SESSION_KEY: "", WORKER_TOKEN: ""}')" ]] ||
  fail "template app did not print the keys of all three families"
printf 'ok: secrets.sh template\n'

secrets_sh check "$env" >"$tmp/check.out" && fail "check passed with empty secrets"
grep -q 'skynet/staging/app is missing: DATABASE_URL SESSION_KEY WORKER_TOKEN' "$tmp/check.out" ||
  fail "check did not list the missing app keys"

printf '{"DATABASE_URL":"db","SESSION_KEY":"","WORKER_TOKEN":"w","EXTRA":"x"}' >"$tmp/app.json"
printf '{"DATABASE_URL":"db"}' >"$tmp/short.json"
printf '{"NEXT_SECRET":"n"}' >"$tmp/frontend.json"
printf '{"LITELLM_MASTER_KEY":"k"}' >"$tmp/litellm.json"
secrets_sh put "$env" app "$tmp/short.json" 2>"$tmp/put.err" && fail "put accepted a file with missing keys"
grep -q 'missing keys in .*: SESSION_KEY WORKER_TOKEN' "$tmp/put.err" || fail "put did not name the missing keys"
for name in app frontend litellm; do
  secrets_sh put "$env" "$name" "$tmp/$name.json" >/dev/null || fail "put $name failed"
done
[[ "$(aws secretsmanager get-secret-value --secret-id "skynet/$env/app" --query SecretString --output text)" == \
  "$(cat "$tmp/app.json")" ]] || fail "put did not store the file"
printf 'ok: secrets.sh put\n'

secrets_sh check "$env" >"$tmp/check.out" || fail "check failed with all keys present"
[[ "$(grep -c 'all keys present' "$tmp/check.out")" == 3 ]] || fail "check did not pass all three secrets"
printf 'ok: secrets.sh check\n'

secrets_sh check-empty "$env" >"$tmp/empty.out" && fail "check-empty passed with an empty value"
grep -q 'skynet/staging/app has empty values: SESSION_KEY' "$tmp/empty.out" ||
  fail "check-empty did not name the empty key"

printf 'session\n' | secrets_sh set "$env" app SESSION_KEY >/dev/null || fail "set failed"
[[ "$(aws secretsmanager get-secret-value --secret-id "skynet/$env/app" --query SecretString --output text |
  jq -c 'to_entries | map(.key) | sort')" == '["DATABASE_URL","EXTRA","SESSION_KEY","WORKER_TOKEN"]' ]] ||
  fail "set did not keep the other keys"
secrets_sh check-empty "$env" >/dev/null || fail "check-empty failed after set"
printf '' | secrets_sh set "$env" app SESSION_KEY 2>/dev/null && fail "set accepted an empty value"
printf 'x\n' | secrets_sh set "$env" app bad-key 2>/dev/null && fail "set accepted a bad key name"

# A secret with no version yet: set starts from {}.
reset_secret litellm >/dev/null
printf 'master\n' | secrets_sh set "$env" litellm LITELLM_MASTER_KEY >/dev/null || fail "set on a new secret failed"
[[ "$(aws secretsmanager get-secret-value --secret-id "skynet/$env/litellm" --query SecretString --output text |
  jq -c 'keys')" == '["LITELLM_MASTER_KEY"]' ]] || fail "set on a new secret did not start from {}"
printf 'ok: secrets.sh check-empty, set\n'

printf 'PASS: test-aws-emulator.sh\n'
