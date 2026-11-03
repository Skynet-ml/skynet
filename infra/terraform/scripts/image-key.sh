#!/usr/bin/env bash
# Keys include the clean Git tree, external FROM digests, linux/arm64, build
# args, the UTC week, and the buildx/BuildKit versions. Empty output means build.
# ctx- keeps every build arg; tree- drops only GIT_SHA for preview reuse.
# The weekly cache rotation bounds unpinned RUN-time network inputs to one week;
# the first build each week is cold, so this is a documented freshness gap.
KEY_VERSION=4

IMAGE_KEY_CTX=""
IMAGE_KEY_TREE=""
IMAGE_BUILD_CONTEXT_ARGS=()
IMAGE_PINNED_DIGESTS=()

_clear_image_inputs() {
  IMAGE_KEY_CTX=""
  IMAGE_KEY_TREE=""
  IMAGE_BUILD_CONTEXT_ARGS=()
  IMAGE_PINNED_DIGESTS=()
}

_is_stage() {
  local value="$1" stages="$2" stage_count="$3"
  [[ $'\n'"$stages"$'\n' == *$'\n'"$value"$'\n'* ]] && return 0
  [[ "$value" =~ ^[0-9]+$ ]] && ((value < stage_count))
}

_hash_image_key() {
  local prefix="$1" context_hash="$2" buildx_version="$3" buildkit_version="$4" week="$5"
  shift 5
  local -a args=("$@") sorted_args=()
  local line digest
  if ((${#args[@]})); then
    while IFS= read -r line; do sorted_args+=("$line"); done < <(
      printf '%s\n' "${args[@]+"${args[@]}"}" | LC_ALL=C sort
    )
  fi
  {
    printf 'version=%s\nweek=%s\nplatform=linux/arm64\ncontext=%s\nbuildx=%s\nbuildkit=%s\n' \
      "$KEY_VERSION" "$week" "$context_hash" "$buildx_version" "$buildkit_version"
    for digest in "${IMAGE_PINNED_DIGESTS[@]+"${IMAGE_PINNED_DIGESTS[@]}"}"; do
      printf 'base=%s\n' "$digest"
    done
    for line in "${sorted_args[@]+"${sorted_args[@]}"}"; do
      printf 'arg=%s\n' "$line"
    done
  } | if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi | cut -c1-40 |
    { read -r line; printf '%s-%s\n' "$prefix" "$line"; }
}

_image_inputs_impl() {
  local context_dir="$1" week="$2" repo_root context_path status dockerfile
  shift 2
  local -a build_args=("$@") tree_args=() words=() base_digests=()
  local line trimmed logical instruction ref image_ref alias digest_json digest
  local stage_count=0 index from_ref source buildx_version builder_info buildkit_version
  local continuation=0 context_hash
  declare -A digest_by_ref=()
  _clear_image_inputs
  [[ "$week" =~ ^[0-9]{4}-W[0-9]{2}$ ]] || return 1
  context_dir="$(cd "$context_dir" && pwd -P)" || return 1
  repo_root="$(git -C "$context_dir" rev-parse --show-toplevel 2>/dev/null)" || return 1
  context_path="${context_dir#"$repo_root"/}"
  [[ "$context_path" != "$context_dir" && -n "$context_path" ]] || return 1
  status="$(git -C "$repo_root" status --porcelain --ignored -- "$context_path" 2>/dev/null)" || return 1
  [[ -z "$status" ]] || return 1
  context_hash="$(git -C "$repo_root" rev-parse "HEAD:$context_path" 2>/dev/null)" || return 1
  dockerfile="$context_dir/Dockerfile"
  [[ -f "$dockerfile" ]] || return 1

  logical=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    trimmed="${line%$'\r'}"
    trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ "$trimmed" =~ ^#[[:space:]]*syntax= ]] && return 1
    [[ -z "$trimmed" || "$trimmed" == \#* ]] && continue
    if ((continuation)); then
      logical+=" $trimmed"
    else
      logical="$trimmed"
    fi
    if [[ "$trimmed" == *\\ ]]; then
      if ((!continuation)); then
        read -r instruction _ <<<"$trimmed"
        [[ "${instruction,,}" == add ]] && return 1
      fi
      logical="${logical%\\}"
      continuation=1
      continue
    fi
    continuation=0
    words=()
    read -r -a words <<<"$logical"
    logical=""
    instruction="${words[0]:-}"
    instruction="${instruction,,}"
    for ((index = 0; index < ${#words[@]}; index++)); do
      case "${words[index]}" in
        --from=*)
          from_ref="${words[index]#--from=}"
          _is_stage "$from_ref" "$(
            printf '%s' "${IMAGE_KNOWN_STAGES:-}"
          )" "$stage_count" || return 1
          ;;
        --from)
          index=$((index + 1))
          from_ref="${words[index]:-}"
          [[ -n "$from_ref" ]] || return 1
          _is_stage "$from_ref" "$(
            printf '%s' "${IMAGE_KNOWN_STAGES:-}"
          )" "$stage_count" || return 1
          ;;
        --mount=*)
          if [[ "${words[index]}" == *from=* ]]; then
            if [[ "${words[index]}" =~ (^|,)from=([^,[:space:]]+) ]]; then
              from_ref="${BASH_REMATCH[2]}"
              _is_stage "$from_ref" "$(
                printf '%s' "${IMAGE_KNOWN_STAGES:-}"
              )" "$stage_count" || return 1
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
          index=$((index + 1))
        done
        ref="${words[index]:-}"
        [[ -n "$ref" && "$ref" != *'$'* ]] || return 1
        if ! _is_stage "$ref" "$IMAGE_KNOWN_STAGES" "$stage_count" && [[ "$ref" != scratch ]]; then
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
          IMAGE_PINNED_DIGESTS+=("$digest")
          IMAGE_BUILD_CONTEXT_ARGS+=("--build-context" "$image_ref=docker-image://$image_ref@$digest")
        fi
        stage_count=$((stage_count + 1))
        if [[ "${words[index + 1]:-}" == [Aa][Ss] && -n "${words[index + 2]:-}" ]]; then
          alias="${words[index + 2],,}"
          IMAGE_KNOWN_STAGES+="$alias"$'\n'
        fi
        ;;
      copy | add)
        index=1
        while [[ "${words[index]:-}" == --* ]]; do index=$((index + 1)); done
        [[ "${words[index]:-}" != \[* ]] || return 1
        if [[ "$instruction" == add ]]; then
          for ((index = 1; index < ${#words[@]} - 1; index++)); do
            source="${words[index]}"
            [[ "$source" == --* ]] && continue
            [[ "$source" == *http://* || "$source" == *https://* || "$source" == *git://* ||
              "$source" == *git@* || "$source" == *.git* || "$source" == *git+* ]] && return 1
          done
        fi
        ;;
    esac
  done <"$dockerfile"
  ((continuation == 0)) || return 1
  ((${#base_digests[@]} > 0)) || return 1

  buildx_version="$(docker buildx version 2>/dev/null)" || return 1
  [[ -n "$buildx_version" ]] || return 1
  builder_info="$(docker buildx inspect --bootstrap 2>/dev/null)" || return 1
  buildkit_version="$(awk '/BuildKit version:/ {print $3}' <<<"$builder_info" | LC_ALL=C sort -u | tr '\n' ',')"
  buildkit_version="${buildkit_version%,}"
  [[ -n "$buildkit_version" ]] || return 1
  IMAGE_KNOWN_STAGES=""

  for line in "${build_args[@]+"${build_args[@]}"}"; do
    [[ "$line" == GIT_SHA=* ]] || tree_args+=("$line")
  done
  IMAGE_KEY_CTX="$(_hash_image_key ctx "$context_hash" "$buildx_version" "$buildkit_version" \
    "$week" "${build_args[@]+"${build_args[@]}"}")"
  IMAGE_KEY_TREE="$(_hash_image_key tree "$context_hash" "$buildx_version" "$buildkit_version" \
    "$week" "${tree_args[@]+"${tree_args[@]}"}")"
}

image_inputs() {
  _clear_image_inputs
  IMAGE_KNOWN_STAGES=""
  if (($# < 2)); then
    return 0
  fi
  local context_dir="$1" week="$2"
  shift 2
  if _image_inputs_impl "$context_dir" "$week" "$@"; then
    IMAGE_KNOWN_STAGES=""
    return 0
  fi
  _clear_image_inputs
  IMAGE_KNOWN_STAGES=""
  return 0
}

image_key() {
  local context_dir="${1:-}" prefix="${2:-}" week
  if (($# < 2)); then
    _clear_image_inputs
    printf '\n'
    return 0
  fi
  shift 2
  case "$prefix" in
    ctx | tree) ;;
    *)
      _clear_image_inputs
      printf '\n'
      return 0
      ;;
  esac
  if ! week="$(date -u +%G-W%V)"; then
    _clear_image_inputs
    printf '\n'
    return 0
  fi
  image_inputs "$context_dir" "$week" "$@"
  case "$prefix" in
    ctx) printf '%s\n' "$IMAGE_KEY_CTX" ;;
    tree) printf '%s\n' "$IMAGE_KEY_TREE" ;;
    *) printf '\n' ;;
  esac
}

verify_image_sources() {
  local metadata_file="$1" digest
  [[ -s "$metadata_file" && ${#IMAGE_PINNED_DIGESTS[@]} -gt 0 ]] || return 1
  for digest in "${IMAGE_PINNED_DIGESTS[@]+"${IMAGE_PINNED_DIGESTS[@]}"}"; do
    jq -e --arg digest "${digest#sha256:}" '
      (.["buildx.build.provenance"].materials // []) as $materials
      | ($materials | type == "array") and any($materials[]; .digest.sha256 == $digest)
    ' "$metadata_file" >/dev/null 2>&1 || return 1
  done
}

find_by_key() {
  aws ecr describe-images --repository-name "$1" --image-ids "imageTag=$2" >/dev/null 2>&1
}

find_fresh_by_key() {
  local repo="$1" key="$2" pushed pushed_at
  pushed="$(aws ecr describe-images --repository-name "$repo" --image-ids "imageTag=$key" \
    --query 'imageDetails[0].imagePushedAt' --output text 2>/dev/null)" || return 1
  [[ -n "$pushed" && "$pushed" != None && "$pushed" != null ]] || return 1
  pushed_at="$(date_epoch "$pushed")" || return 1
  (( $(date +%s) - pushed_at < 11 * 86400 ))
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
  printf '%s\n' "${pages[@]+"${pages[@]}"}" | jq -s -r 'add | sort_by(.pushedAt) | last.tag // empty'
}
