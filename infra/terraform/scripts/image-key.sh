#!/usr/bin/env bash

# Keys include the clean Git tree, resolved external FROM digests, linux/arm64,
# and sorted build args. Empty output means the caller must build. A ctx- key
# includes build args; a tree- key omits them for preview reuse.
# Fresh reuse is limited to 11 days because RUN-time network inputs are not in
# the key; it also stays within skynet/backend's newest-100-tag lifecycle.
KEY_VERSION=3

IMAGE_KEY=""
IMAGE_BUILD_CONTEXT_ARGS=()

_is_stage() {
  local value="$1" stages="$2" stage_count="$3"
  [[ $'\n'"$stages"$'\n' == *$'\n'"$value"$'\n'* ]] && return 0
  [[ "$value" =~ ^[0-9]+$ ]] && ((value < stage_count))
}

_image_inputs_impl() {
  local context_dir="$1" prefix="${2:-ctx}" repo_root context_path status dockerfile
  shift 2
  local -a build_args=("$@") words base_digests=() sorted_args=()
  local line trimmed instruction ref image_ref alias digest_json digest
  local stages="" stage_count=0 index from_ref source
  declare -A digest_by_ref=()
  IMAGE_KEY=""
  IMAGE_BUILD_CONTEXT_ARGS=()
  [[ "$prefix" == ctx || "$prefix" == tree ]] || return 1
  [[ "$prefix" == tree ]] && build_args=()
  context_dir="$(cd "$context_dir" && pwd -P)" || return 1
  repo_root="$(git -C "$context_dir" rev-parse --show-toplevel 2>/dev/null)" || return 1
  context_path="${context_dir#"$repo_root"/}"
  [[ "$context_path" != "$context_dir" && -n "$context_path" ]] || return 1
  status="$(git -C "$repo_root" status --porcelain --ignored -- "$context_path" 2>/dev/null)" || return 1
  [[ -z "$status" ]] || return 1
  git -C "$repo_root" rev-parse "HEAD:$context_path" >/dev/null 2>&1 || return 1
  dockerfile="$context_dir/Dockerfile"
  [[ -f "$dockerfile" ]] || return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [[ "$trimmed" =~ ^#[[:space:]]*syntax= ]] && return 1
    [[ "$trimmed" == \#* || -z "$trimmed" ]] && continue
    if [[ "${trimmed%$'\r'}" == *\\ ]]; then
      instruction="${trimmed%%[[:space:]]*}"
      [[ "${instruction,,}" == from || "${instruction,,}" == add ]] && return 1
    fi
    line="${line%%#*}"
    read -r -a words <<<"$line"
    instruction="${words[0]:-}"
    instruction="${instruction,,}"
    for ((index = 0; index < ${#words[@]}; index++)); do
      case "${words[index]}" in
        --from=*)
          from_ref="${words[index]#--from=}"
          _is_stage "$from_ref" "$stages" "$stage_count" || return 1
          ;;
        --from)
          index=$((index + 1))
          from_ref="${words[index]:-}"
          [[ -n "$from_ref" ]] || return 1
          _is_stage "$from_ref" "$stages" "$stage_count" || return 1
          ;;
        --mount=*)
          if [[ "${words[index]}" == *from=* ]]; then
            if [[ "${words[index]}" =~ (^|,)from=([^,[:space:]]+) ]]; then
              from_ref="${BASH_REMATCH[2]}"
              _is_stage "$from_ref" "$stages" "$stage_count" || return 1
            else
              return 1
            fi
          fi
          ;;
      esac
    done
    case "$instruction" in
      from)
        index=1
        while [[ "${words[index]:-}" == --* ]]; do
          [[ "${words[index]}" == --platform=linux/arm64 ]] || return 1
          ((index++))
        done
        ref="${words[index]:-}"
        [[ -n "$ref" && "$ref" != *'$'* ]] || return 1
        if ! _is_stage "$ref" "$stages" "$stage_count" && [[ "$ref" != scratch ]]; then
          image_ref="$ref"
          if [[ -v "digest_by_ref[$image_ref]" ]]; then
            digest="${digest_by_ref[$image_ref]}"
          else
            digest_json="$(docker buildx imagetools inspect "$image_ref" \
              --format '{{json .Manifest.Digest}}' 2>/dev/null)" || return 1
            digest="$(jq -er 'select(type == "string" and test("^sha256:[0-9a-f]{64}$"))' \
              <<<"$digest_json" 2>/dev/null)" || return 1
            digest_by_ref["$image_ref"]="$digest"
          fi
          base_digests+=("$image_ref@$digest")
          IMAGE_BUILD_CONTEXT_ARGS+=("--build-context" "$image_ref=docker-image://$image_ref@$digest")
        fi
        ((stage_count++))
        if [[ "${words[index + 1]:-}" == [Aa][Ss] && -n "${words[index + 2]:-}" ]]; then
          alias="${words[index + 2],,}"
          stages+="$alias"$'\n'
        fi
        ;;
      copy|add)
        if [[ "$instruction" == add ]]; then
          for source in "${words[@]:1:${#words[@]}-2}"; do
            [[ "$source" == --* ]] && continue
            [[ "$source" == http://* || "$source" == https://* || "$source" == git://* ||
              "$source" == git@* || "$source" == *.git || "$source" == git+* ]] && return 1
          done
        fi
        ;;
    esac
  done <"$dockerfile"
  ((${#base_digests[@]} > 0)) || return 1

  while IFS= read -r line; do sorted_args+=("$line"); done < <(
    printf '%s\n' "${build_args[@]}" | LC_ALL=C sort
  )
  IMAGE_KEY="$(
    {
      printf 'version=%s\nplatform=linux/arm64\ncontext=%s\n' "$KEY_VERSION" \
        "$(git -C "$repo_root" rev-parse "HEAD:$context_path")"
      printf 'base=%s\n' "${base_digests[@]}"
      printf 'arg=%s\n' "${sorted_args[@]}"
    } | if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi | cut -c1-40
  )"
  IMAGE_KEY="$prefix-${IMAGE_KEY}"
}

image_inputs() {
  IMAGE_KEY=""
  IMAGE_BUILD_CONTEXT_ARGS=()
  if _image_inputs_impl "$@"; then
    return 0
  fi
  IMAGE_KEY=""
  IMAGE_BUILD_CONTEXT_ARGS=()
  return 0
}

image_key() {
  image_inputs "$@"
  printf '%s\n' "$IMAGE_KEY"
}

find_by_key() {
  aws ecr describe-images --repository-name "$1" --image-ids "imageTag=$2" >/dev/null 2>&1
}

find_fresh_by_key() {
  local repo="$1" key="$2" pushed pushed_at
  pushed="$(aws ecr describe-images --repository-name "$repo" --image-ids "imageTag=$key" \
    --query 'imageDetails[0].imagePushedAt' --output text 2>/dev/null)" || return 1
  [[ -n "$pushed" && "$pushed" != None && "$pushed" != null ]] || return 1
  pushed_at="$(date -d "$pushed" +%s 2>/dev/null)" || return 1
  if (( $(date +%s) - pushed_at < 11 * 86400 )); then
    return 0
  fi
  aws ecr batch-delete-image --repository-name "$repo" --image-ids "imageTag=$key" >/dev/null 2>&1 || true
  return 1
}

retag() {
  local repo="$1" existing="$2" new="$3" response manifest media_type error
  local manifest_digest existing_digest
  response="$(aws ecr batch-get-image --repository-name "$repo" --image-ids "imageTag=$existing" 2>/dev/null)" ||
    return 1
  manifest="$(jq -er '.images[0].imageManifest | select(type == "string")' <<<"$response" 2>/dev/null)" || return 1
  manifest_digest="$(jq -er '.images[0].imageId.imageDigest | select(type == "string")' <<<"$response" 2>/dev/null)" ||
    return 1
  media_type="$(jq -r '.images[0].imageManifestMediaType // empty' <<<"$response" 2>/dev/null)" || return 1
  if [[ -n "$media_type" ]]; then
    error="$(aws ecr put-image --repository-name "$repo" --image-tag "$new" \
      --image-manifest "$manifest" --image-manifest-media-type "$media_type" 2>&1)" && return 0
  else
    error="$(aws ecr put-image --repository-name "$repo" --image-tag "$new" --image-manifest "$manifest" 2>&1)" &&
      return 0
  fi
  if [[ "$error" == *ImageAlreadyExistsException* || "$error" == *ImageTagAlreadyExistsException* ]]; then
    existing_digest="$(aws ecr describe-images --repository-name "$repo" --image-ids "imageTag=$new" \
      --query 'imageDetails[0].imageDigest' --output text 2>/dev/null)" || return 1
    [[ "$existing_digest" == "$manifest_digest" ]] && return 0
  fi
  printf '%s\n' "$error" >&2
  return 1
}

latest_cache_ref() {
  local repo="$1" prefix="$2" token="" response page next
  local -a pages=()
  while :; do
    if [[ -n "$token" ]]; then
      response="$(aws ecr describe-images --repository-name "$repo" --filter tagStatus=TAGGED \
        --starting-token "$token" --output json 2>/dev/null)" || return 0
    else
      response="$(aws ecr describe-images --repository-name "$repo" --filter tagStatus=TAGGED \
        --output json 2>/dev/null)" || return 0
    fi
    page="$(jq -c --arg prefix "$prefix" '
      [.imageDetails[]? as $image | $image.imageTags[]? |
       select(startswith($prefix)) | {tag: ., pushedAt: $image.imagePushedAt}]' <<<"$response" 2>/dev/null)" ||
      return 0
    pages+=("$page")
    next="$(jq -r '.nextToken // empty' <<<"$response" 2>/dev/null)" || return 0
    [[ -n "$next" ]] || break
    token="$next"
  done
  printf '%s\n' "${pages[@]}" | jq -s -r 'add | sort_by(.pushedAt) | last.tag // empty'
}
