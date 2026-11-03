#!/usr/bin/env bash
# Builds the three images for linux/arm64 (Graviton) and pushes them to ECR.
#
#   infra/terraform/scripts/build-push.sh <staging|production> [git-sha] [--migrate]
#
# Tags: backend and litellm get <sha>; the frontend gets <sha>-<env>, because
# Next.js bakes NEXT_PUBLIC_* values into the bundle at build time.
# Application image tags are immutable in ECR, so an existing tag is skipped.
#
# Frontend build values come from the environment of this shell:
#   NEXT_PUBLIC_SENTRY_DSN, NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE,
#   NEXT_PUBLIC_FEATURE_GENERALIST_AGENT (optional)

source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/image-key.sh"
need aws docker git jq sort tr date
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || die "missing command: sha256sum or shasum"

env="${1:-}"
check_env "$env"
repo_root="$(git rev-parse --show-toplevel)"
sha="${2:-$(git -C "$repo_root" rev-parse HEAD)}"
[[ -z "${3:-}" || "${3:-}" == --migrate ]] || die "usage: build-push.sh <environment> [git-sha] [--migrate]"
migrate_requested="${3:-}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
marker="$(mktemp)"
trap 'rm -f "$marker"' EXIT

case "$env" in
  staging) site_url="https://staging.skynetml.com" ;;
  production) site_url="https://skynetml.com" ;;
esac

# ECR URLs come from the global stack, so this runs before the environment exists.
cfg="$(aws ecr describe-repositories --repository-names skynet/backend skynet/frontend skynet/litellm \
  --query 'repositories[].[repositoryName,repositoryUri]' --output json |
  jq '{ecr: (map({key: (.[0] | ltrimstr("skynet/")), value: .[1]}) | from_entries)}')"
registry="$(jq -r '.ecr.backend' <<<"$cfg" | cut -d/ -f1)"
aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null

build() {
  local name="$1" tag="$2" context="$3" cache_name="$4"
  shift 4
  local url key cache_tag cache_repo
  local -a build_args=("$@")
  local -a cache_from=()
  url="$(jq -r --arg n "$name" '.ecr[$n]' <<<"$cfg")"
  cache_repo="skynet/buildcache-$env"
  if find_by_key "skynet/$name" "$tag"; then
    echo "skip: $url:$tag exists"
    LAST_BUILD_BUILT=0
    return 0
  fi

  image_inputs "$context" ctx "${build_args[@]}"
  key="$IMAGE_KEY"
  local -a pin_args=("${IMAGE_BUILD_CONTEXT_ARGS[@]}")
  if [[ -n "$key" ]] && find_fresh_by_key "skynet/$name" "$key"; then
    if retag "skynet/$name" "$key" "$tag"; then
      echo "reuse: $url:$key as $tag"
      LAST_BUILD_BUILT=0
      return 0
    fi
    echo "warning: cannot retag $url:$key as $tag; building" >&2
  fi

  cache_tag="$(latest_cache_ref "$cache_repo" "$cache_name-")"
  [[ -n "$cache_tag" ]] && cache_from+=(--cache-from "type=registry,ref=$registry/$cache_repo:$cache_tag")
  if ! docker buildx build --platform linux/arm64 --provenance=false \
    "${cache_from[@]}" \
    --cache-to "type=registry,ref=$registry/$cache_repo:$cache_name-$sha,mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true" \
    -t "$url:$tag" --push "${pin_args[@]}" "${build_args[@]}" "$context"; then
    # Staging and production can build the same backend and litellm tag at
    # once; the second push then fails on the immutable tag.
    find_by_key "skynet/$name" "$tag" || die "push failed: $url:$tag"
    echo "skip: $url:$tag was pushed by another run"
    LAST_BUILD_BUILT=0
    return 0
  fi

  LAST_BUILD_BUILT=1
  if [[ -n "$key" ]] && ! retag "skynet/$name" "$tag" "$key"; then
    echo "warning: cannot add image-key tag $url:$key" >&2
  fi
}

build_backend_lane() {
  local key
  build backend "$sha" "$repo_root/backend" backend --build-arg "GIT_SHA=$sha"
  if ((LAST_BUILD_BUILT)); then
    image_inputs "$repo_root/backend" tree
    key="$IMAGE_KEY"
    if [[ -n "$key" ]]; then
      if ! retag skynet/backend "$sha" "$key"; then
        echo "warning: cannot add tree image-key tag skynet/backend:$key" >&2
      fi
    fi
  fi
  if [[ "$migrate_requested" == --migrate ]]; then
    # deploy.sh's rollback path and old tasks serving during rolling updates
    # require migrations to work with previous code before other images finish.
    refresh_aws_credentials
    "$script_dir/deploy.sh" "$env" "$sha" --migrate-only
    printf '%s\n' "$sha" >"$marker"
  fi
}

run_lane() {
  local label="$1"
  shift
  "$@" 2>&1 | sed -u "s/^/[$label] /"
}

run_lane backend build_backend_lane &
backend_pid=$!
run_lane litellm build litellm "$sha" "$repo_root/deploy/litellm" litellm &
litellm_pid=$!
run_lane frontend build frontend "$sha-$env" "$repo_root/frontend" "frontend-$env" \
  --build-arg "NEXT_PUBLIC_SITE_URL=$site_url" \
  --build-arg "NEXT_PUBLIC_SENTRY_DSN=${NEXT_PUBLIC_SENTRY_DSN:-}" \
  --build-arg "NEXT_PUBLIC_SENTRY_ENVIRONMENT=$env" \
  --build-arg "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE=${NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE:-}" \
  --build-arg "NEXT_PUBLIC_FEATURE_GENERALIST_AGENT=${NEXT_PUBLIC_FEATURE_GENERALIST_AGENT:-}" \
  --build-arg "APP_GIT_SHA=$sha" \
  --build-arg "APP_ENVIRONMENT=$env" &
frontend_pid=$!

failed=""
wait "$backend_pid" || failed+=" backend"
if [[ -s "$marker" ]]; then migrated=true; else migrated=false; fi
wait "$litellm_pid" || failed+=" litellm"
wait "$frontend_pid" || failed+=" frontend"
if [[ -n "$failed" ]]; then
  if [[ "$migrated" == true &&
    ( "$failed" == *" litellm"* || "$failed" == *" frontend"* ) ]]; then
    echo "::error::database migrated to $sha; services not updated (old code runs against the new schema, as after a rollback)" >&2
  fi
  die "image build or migration failed:$failed"
fi
if [[ "$migrated" == true && -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'migrated=%s\n' "$sha" >>"$GITHUB_OUTPUT"
fi

echo "pushed: backend:$sha litellm:$sha frontend:$sha-$env"
