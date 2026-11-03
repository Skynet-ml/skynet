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
[[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || die "second argument must be a git SHA"
[[ -z "${3:-}" || "${3:-}" == --migrate ]] || die "usage: build-push.sh <environment> [git-sha] [--migrate]"
migrate_requested="${3:-}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
lane_dir="$(mktemp -d)"
marker="$lane_dir/migrated"
trap 'rm -rf "$lane_dir"' EXIT

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
  local url key tree_key cache_tag cache_repo week metadata_file="" build_succeeded
  local -a build_args=("$@") pin_args=() cache_from=() metadata_args=()
  url="$(jq -r --arg n "$name" '.ecr[$n]' <<<"$cfg")" || return 1
  [[ -n "$url" && "$url" != null ]] || return 1
  cache_repo="skynet/buildcache-$env"
  week="$(date -u +%G-W%V)" || return 1
  [[ "$week" =~ ^[0-9]{4}-W[0-9]{2}$ ]] || return 1
  image_inputs "$context" "$week" "${build_args[@]+"${build_args[@]}"}"
  key="$IMAGE_KEY_CTX"
  tree_key="$IMAGE_KEY_TREE"
  pin_args=("${IMAGE_BUILD_CONTEXT_ARGS[@]+"${IMAGE_BUILD_CONTEXT_ARGS[@]}"}")
  if find_by_key "skynet/$name" "$tag"; then
    echo "skip: $url:$tag exists"
    return 0
  fi

  if [[ -n "$key" ]] && find_fresh_by_key "skynet/$name" "$key"; then
    if retag "skynet/$name" "$key" "$tag"; then
      echo "reuse: $url:$key as $tag"
      return 0
    fi
    echo "warning: cannot retag $url:$key as $tag; building" >&2
  fi

  if [[ -z "$key" ]]; then
    echo "warning: image inputs could not be verified for $url; building without keyed image reuse" >&2
  fi
  cache_tag="$(latest_cache_ref "$cache_repo" "$cache_name-$week-")" || return 1
  if [[ -n "$cache_tag" ]]; then
    cache_from+=(--cache-from "type=registry,ref=$registry/$cache_repo:$cache_tag")
  fi
  if [[ -n "$key" ]]; then
    metadata_file="$(mktemp)" || return 1
    [[ -n "$metadata_file" ]] || return 1
    metadata_args+=(--metadata-file "$metadata_file")
  fi
  refresh_aws_credentials || return 1
  aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null || return 1
  if BUILDX_METADATA_PROVENANCE=min docker buildx build --platform linux/arm64 --provenance=false \
    "${metadata_args[@]+"${metadata_args[@]}"}" \
    "${cache_from[@]+"${cache_from[@]}"}" \
    --cache-to "type=registry,ref=$registry/$cache_repo:$cache_name-$week-$sha,mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true" \
    -t "$url:$tag" --push "${pin_args[@]+"${pin_args[@]}"}" \
    "${build_args[@]+"${build_args[@]}"}" "$context"; then
    build_succeeded=true
  else
    build_succeeded=false
  fi
  refresh_aws_credentials || return 1
  if [[ "$build_succeeded" != true ]]; then
    [[ -z "$metadata_file" ]] || rm -f "$metadata_file"
    # Staging and production can build the same backend and litellm tag at
    # once; the second push then fails on the immutable tag.
    find_by_key "skynet/$name" "$tag" || die "push failed: $url:$tag"
    echo "skip: $url:$tag was pushed by another run"
    return 0
  fi

  if [[ -n "$key" ]]; then
    if verify_image_sources "$metadata_file"; then
      if ! retag "skynet/$name" "$tag" "$key"; then
        echo "warning: cannot add image-key tag $url:$key" >&2
      fi
      if [[ "$name" == backend && -n "$tree_key" ]] &&
        ! retag skynet/backend "$tag" "$tree_key"; then
        echo "warning: cannot add tree image-key tag skynet/backend:$tree_key" >&2
      fi
    else
      echo "::warning::build metadata did not verify every pinned source; no image-key tags added for $url:$tag" >&2
    fi
  fi
  [[ -z "$metadata_file" ]] || rm -f "$metadata_file"
}

build_backend_lane() {
  build backend "$sha" "$repo_root/backend" backend --build-arg "GIT_SHA=$sha" || return 1
  if [[ "$migrate_requested" == --migrate ]]; then
    wait_for_lane_success litellm "$litellm_pid" frontend "$frontend_pid" ||
      return 1
    # Existing tasks stay live until deploy.sh rolls services, so migrations
    # must remain compatible with the previous application code.
    refresh_aws_credentials || return 1
    "$script_dir/deploy.sh" "$env" "$sha" --migrate-only || return 1
    printf '%s\n' "$sha" >"$marker" || return 1
  fi
}

run_image_lane() {
  local lane="$1"
  shift
  "$@"
  : >"$lane_dir/$lane.ok"
}

wait_for_lane_success() {
  local lane pid state index all_done
  local -a lane_pids=("$@")
  while :; do
    all_done=true
    for ((index = 0; index < ${#lane_pids[@]}; index += 2)); do
      lane="${lane_pids[index]}"
      pid="${lane_pids[index + 1]:-}"
      [[ -n "$lane" && "$pid" =~ ^[0-9]+$ ]] || return 1
      [[ ! -e "$lane_dir/$lane.failed" ]] || return 1
      [[ -e "$lane_dir/$lane.ok" ]] && continue
      all_done=false
      kill -0 "$pid" 2>/dev/null || return 1
      state="$(ps -o stat= -p "$pid" 2>/dev/null)" || return 1
      [[ -n "$state" && "$state" != Z* ]] || return 1
    done
    [[ "$all_done" == true ]] && return 0
    sleep 1 || return 1
  done
}

run_lane() {
  local label="$1"
  shift
  trap '_mark_lane_failure "$?" "$label"' EXIT
  "$@" > >(sed -u "s/^/[$label] /") 2>&1
}

_mark_lane_failure() {
  local status="$1" label="$2"
  if ((status != 0)); then
    : >"$lane_dir/$label.failed"
  fi
}

run_lane litellm run_image_lane litellm build litellm "$sha" "$repo_root/deploy/litellm" litellm &
litellm_pid=$!
run_lane frontend run_image_lane frontend build frontend "$sha-$env" "$repo_root/frontend" "frontend-$env" \
  --build-arg "NEXT_PUBLIC_SITE_URL=$site_url" \
  --build-arg "NEXT_PUBLIC_SENTRY_DSN=${NEXT_PUBLIC_SENTRY_DSN:-}" \
  --build-arg "NEXT_PUBLIC_SENTRY_ENVIRONMENT=$env" \
  --build-arg "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE=${NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE:-}" \
  --build-arg "NEXT_PUBLIC_FEATURE_GENERALIST_AGENT=${NEXT_PUBLIC_FEATURE_GENERALIST_AGENT:-}" \
  --build-arg "APP_GIT_SHA=$sha" \
  --build-arg "APP_ENVIRONMENT=$env" &
frontend_pid=$!
run_lane backend run_image_lane backend build_backend_lane &
backend_pid=$!

failed=""
wait "$backend_pid" || failed+=" backend"
if [[ -s "$marker" ]]; then migrated=true; else migrated=false; fi
wait "$litellm_pid" || failed+=" litellm"
wait "$frontend_pid" || failed+=" frontend"
if [[ -n "$failed" ]]; then
  die "image build or migration failed:$failed"
fi
if [[ "$migrated" == true && -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'migrated=%s\n' "$sha" >>"$GITHUB_OUTPUT"
fi

echo "pushed: backend:$sha litellm:$sha frontend:$sha-$env"
