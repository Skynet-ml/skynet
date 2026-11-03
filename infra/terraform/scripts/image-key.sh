#!/usr/bin/env bash

KEY_VERSION=1

image_key() {
  local context_dir repo_root context_path status tree_hash dockerfile line
  local ref alias image_ref stages="" digest digest_json key
  local -a build_args=() digests=() words sorted_args
  context_dir="$(cd "$1" && pwd -P)" || return 0
  shift
  build_args=("$@")
  repo_root="$(git -C "$context_dir" rev-parse --show-toplevel 2>/dev/null)" || return 0
  context_path="${context_dir#"$repo_root"/}"
  [[ "$context_path" != "$context_dir" && -n "$context_path" ]] || return 0
  status="$(git -C "$repo_root" status --porcelain -- "$context_path" 2>/dev/null)" || return 0
  [[ -z "$status" ]] || return 0
  tree_hash="$(git -C "$repo_root" rev-parse "HEAD:$context_path" 2>/dev/null)" || return 0
  dockerfile="$context_dir/Dockerfile"
  [[ -f "$dockerfile" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    read -r -a words <<<"$line"
    [[ "${words[0]:-}" == [Ff][Rr][Oo][Mm] ]] || continue
    ref="${words[1]:-}"
    if [[ "$ref" == --platform=* ]]; then
      ref="${words[2]:-}"
    fi
    [[ -n "$ref" && "$ref" != *'$'* ]] || return 0
    image_ref="$ref"
    ref="$(printf '%s' "$ref" | tr '[:upper:]' '[:lower:]')"
    if [[ $'\n'"$stages"$'\n' == *$'\n'"$ref"$'\n'* ]]; then
      continue
    fi
    digest_json="$(docker buildx imagetools inspect "$image_ref" --format '{{json .Manifest.Digest}}' 2>/dev/null)" ||
      return 0
    digest="$(jq -er 'select(type == "string" and test("^sha256:[0-9a-f]{64}$"))' <<<"$digest_json" 2>/dev/null)" || return 0
    digests+=("$digest")
    if [[ "${words[2]:-}" == [Aa][Ss] && -n "${words[3]:-}" ]]; then
      alias="$(printf '%s' "${words[3]}" | tr '[:upper:]' '[:lower:]')"
      stages+="$alias"$'\n'
    fi
  done <"$dockerfile"
  ((${#digests[@]} > 0)) || return 0

  while IFS= read -r line; do sorted_args+=("$line"); done < <(printf '%s\n' "${build_args[@]}" | LC_ALL=C sort)
  key="$(
    {
      printf 'version=%s\ncontext=%s\n' "$KEY_VERSION" "$tree_hash"
      printf 'base=%s\n' "${digests[@]}"
      printf 'arg=%s\n' "${sorted_args[@]}"
      } | if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi | cut -c1-40
  )" || return 0
  printf 'ctx-%s\n' "$key"
}

find_by_key() {
  aws ecr describe-images --repository-name "$1" --image-ids "imageTag=$2" >/dev/null 2>&1
}

retag() {
  local repo="$1" existing="$2" new="$3" response manifest media_type error
  response="$(aws ecr batch-get-image --repository-name "$repo" --image-ids "imageTag=$existing" 2>/dev/null)" ||
    return 1
  manifest="$(jq -er '.images[0].imageManifest | select(type == "string")' <<<"$response" 2>/dev/null)" || return 1
  media_type="$(jq -r '.images[0].imageManifestMediaType // empty' <<<"$response" 2>/dev/null)" || return 1
  if [[ -n "$media_type" ]]; then
    error="$(aws ecr put-image --repository-name "$repo" --image-tag "$new" \
      --image-manifest "$manifest" --image-manifest-media-type "$media_type" 2>&1)" && return 0
  else
    error="$(aws ecr put-image --repository-name "$repo" --image-tag "$new" --image-manifest "$manifest" 2>&1)" &&
      return 0
  fi
  [[ "$error" == *ImageAlreadyExistsException* ]] && return 0
  printf '%s\n' "$error" >&2
  return 1
}
