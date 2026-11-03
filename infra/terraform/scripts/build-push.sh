#!/usr/bin/env bash
# Builds the three images for linux/arm64 (Graviton) and pushes them to ECR.
#
#   infra/terraform/scripts/build-push.sh <staging|production> [git-sha]
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
need aws docker git jq sort tr
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || die "missing command: sha256sum or shasum"

env="${1:-}"
check_env "$env"
repo_root="$(git rev-parse --show-toplevel)"
sha="${2:-$(git -C "$repo_root" rev-parse HEAD)}"
script_dir="$(cd "$(dirname "$0")" && pwd)"

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
  local url key cache_ref
  local -a build_args=("$@")
  url="$(jq -r --arg n "$name" '.ecr[$n]' <<<"$cfg")"
  cache_ref="$registry/skynet/buildcache:$cache_name"
  if find_by_key "skynet/$name" "$tag"; then
    echo "skip: $url:$tag exists"
    return 0
  fi

  key="$(image_key "$context" ctx "${build_args[@]}")" || key=""
  if [[ -n "$key" ]] && find_by_key "skynet/$name" "$key"; then
    if retag "skynet/$name" "$key" "$tag"; then
      echo "reuse: $url:$key as $tag"
      return 0
    fi
    echo "warning: cannot retag $url:$key as $tag; building" >&2
  fi

  if ! docker buildx build --platform linux/arm64 --provenance=false \
    --cache-from "type=registry,ref=$cache_ref" \
    --cache-to "type=registry,ref=$cache_ref,mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true" \
    -t "$url:$tag" --push "${build_args[@]}" "$context"; then
    # Staging and production can build the same backend and litellm tag at
    # once; the second push then fails on the immutable tag.
    find_by_key "skynet/$name" "$tag" || die "push failed: $url:$tag"
    echo "skip: $url:$tag was pushed by another run"
    return 0
  fi

  if [[ -n "$key" ]] && ! retag "skynet/$name" "$tag" "$key"; then
    echo "warning: cannot add image-key tag $url:$key" >&2
  fi
}

build_backend_lane() {
  local key
  build backend "$sha" "$repo_root/backend" backend --build-arg "GIT_SHA=$sha"
  key="$(image_key "$repo_root/backend" tree)"
  if [[ -n "$key" ]]; then
    if find_by_key skynet/backend "$sha"; then
      if ! retag skynet/backend "$sha" "$key"; then
        echo "warning: cannot add tree image-key tag skynet/backend:$key" >&2
      fi
    else
      echo "warning: cannot find skynet/backend:$sha to add tree image-key tag" >&2
    fi
  else
    echo "warning: cannot compute backend tree image key" >&2
  fi
  # Old tasks serve during the rolling update, so migrations must work with
  # their code. They can run before frontend and LiteLLM images finish.
  "$script_dir/deploy.sh" "$env" "$sha" --migrate-only
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
wait "$litellm_pid" || failed+=" litellm"
wait "$frontend_pid" || failed+=" frontend"
[[ -z "$failed" ]] || die "image build or migration failed:$failed"

echo "pushed: backend:$sha litellm:$sha frontend:$sha-$env"
