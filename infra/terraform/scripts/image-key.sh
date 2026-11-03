#!/usr/bin/env bash
# Keys include Git context bytes, pinned sources, Buildx inputs, build args,
# and a weekly version. Empty output means build; tree- omits GIT_SHA.
# Only environment-scoped tags use ctx-key reuse; backend tree keys support previews.
# Shared backend and LiteLLM tags get key tags only after a fresh build.
# The weekly cache rotation bounds unpinned RUN-time network inputs to one week;
# the first build each week is cold, so this is a documented freshness gap.
KEY_VERSION=6

IMAGE_KEY_CTX=""
IMAGE_KEY_TREE=""
IMAGE_BUILD_CONTEXT_ARGS=()
IMAGE_PINNED_DIGESTS=()
IMAGE_CONTEXT_ATTRIBUTE_HASHES=()
IMAGE_KEY_BUILD_FLAGS=()
IMAGE_KEY_BUILD_DRIVER=""

_clear_image_inputs() {
  IMAGE_KEY_CTX=""
  IMAGE_KEY_TREE=""
  IMAGE_BUILD_CONTEXT_ARGS=()
  IMAGE_PINNED_DIGESTS=()
  IMAGE_CONTEXT_ATTRIBUTE_HASHES=()
  IMAGE_KEY_BUILD_DRIVER=""
}

image_build_flags() {
  local context="$1"
  IMAGE_KEY_BUILD_FLAGS=(--platform linux/arm64 --provenance=false --progress=plain --file "$context/Dockerfile")
  export BUILDX_METADATA_PROVENANCE=min
}

_is_stage() {
  local value="$1" stages="$2" stage_count="$3"
  [[ $'\n'"$stages"$'\n' == *$'\n'"$value"$'\n'* ]] && return 0
  [[ "$value" =~ ^[0-9]+$ ]] && ((value < stage_count))
}

_hash_context_attributes() {
  local repo_root="$1" context_path="$2" files_file attrs_file path attr value full_path file_hash
  local status=0
  local -a attr_paths=()
  declare -A seen_attr_paths=()
  files_file="$(mktemp)" || return 1
  attrs_file="$(mktemp)" || {
    rm -f "$files_file"
    return 1
  }
  if ! git -C "$repo_root" ls-files -z -- "$context_path" >"$files_file" ||
    ! git -C "$repo_root" check-attr -z --stdin filter eol text ident working-tree-encoding \
      <"$files_file" >"$attrs_file"; then
    rm -f "$files_file" "$attrs_file"
    return 1
  fi
  while IFS= read -r -d '' path; do
    IFS= read -r -d '' attr || {
      status=1
      break
    }
    IFS= read -r -d '' value || {
      status=1
      break
    }
    case "$attr" in
      filter|eol|text|ident|working-tree-encoding) ;;
      *) status=1; break ;;
    esac
    if [[ "$value" != unspecified && "$value" != unset && -z "${seen_attr_paths[$path]:-}" ]]; then
      attr_paths+=("$path")
      seen_attr_paths["$path"]=1
    fi
  done <"$attrs_file"
  if ((status != 0)); then
    rm -f "$files_file" "$attrs_file"
    return 1
  fi
  for path in "${attr_paths[@]+"${attr_paths[@]}"}"; do
    full_path="$repo_root/$path"
    [[ -f "$full_path" ]] || {
      rm -f "$files_file" "$attrs_file"
      return 1
    }
    file_hash="$(
      set -o pipefail
      { printf '%s\0' "$path"; cat -- "$full_path"; } |
        if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi |
        cut -c1-64
    )" || {
      rm -f "$files_file" "$attrs_file"
      return 1
    }
    [[ "$file_hash" =~ ^[0-9a-f]{64}$ ]] || {
      rm -f "$files_file" "$attrs_file"
      return 1
    }
    IMAGE_CONTEXT_ATTRIBUTE_HASHES+=("$path:$file_hash")
  done
  rm -f "$files_file" "$attrs_file"
}

_hash_image_key() {
  local prefix="$1" context_hash="$2" buildx_version="$3" buildkit_version="$4" week="$5"
  shift 5
  local hash
  local -a args=("$@") sorted_args=()
  local line digest name value previous="" umask_value
  umask_value="$(umask)" || return 1
  if ((${#args[@]})); then
    while IFS= read -r line; do sorted_args+=("$line"); done < <(
      printf '%s\n' "${args[@]+"${args[@]}"}" | LC_ALL=C sort
    )
  fi
  local -a env_material=()
  while IFS= read -r name; do
    [[ "$name" == SOURCE_DATE_EPOCH || "$name" == DOCKER_DEFAULT_PLATFORM ||
      "$name" == BUILDX_METADATA_PROVENANCE || "$name" == BUILDX_NO_DEFAULT_ATTESTATIONS ||
      "$name" == BUILDX_GIT_LABELS || "$name" == BUILDX_GIT_INFO ]] || continue
    value="${!name}"
    env_material+=("$name=$value")
  done < <(compgen -e | LC_ALL=C sort)
  hash="$(
    set -o pipefail
    {
      printf 'version=%s\nweek=%s\nplatform=linux/arm64\numask=%s\ncontext=%s\nbuildx=%s\nbuildkit=%s\n' \
        "$KEY_VERSION" "$week" "$umask_value" "$context_hash" "$buildx_version" "$buildkit_version"
      # The pinned setup action supplies defaults; the BuildKit version is hashed.
      printf 'builder-driver=%s\n' "$IMAGE_KEY_BUILD_DRIVER"
      for line in "${IMAGE_KEY_BUILD_FLAGS[@]+"${IMAGE_KEY_BUILD_FLAGS[@]}"}"; do
        if [[ "$previous" == --file ]]; then
          line="${line##*/}"
          previous=""
        elif [[ "$line" == --file ]]; then
          previous=--file
        fi
        printf 'buildx-flag=%s\n' "$line"
      done
      for line in "${env_material[@]+"${env_material[@]}"}"; do
        printf 'buildx-env=%s\n' "$line"
      done
      for line in "${IMAGE_CONTEXT_ATTRIBUTE_HASHES[@]+"${IMAGE_CONTEXT_ATTRIBUTE_HASHES[@]}"}"; do
        printf 'attribute-file=%s\n' "$line"
      done
      for digest in "${IMAGE_PINNED_DIGESTS[@]+"${IMAGE_PINNED_DIGESTS[@]}"}"; do
        printf 'base=%s\n' "$digest"
      done
      for line in "${sorted_args[@]+"${sorted_args[@]}"}"; do
        printf 'arg=%s\n' "$line"
      done
    } | if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi | cut -c1-40
  )" || return 1
  [[ "$hash" =~ ^[0-9a-f]{40}$ ]] || return 1
  printf '%s-%s\n' "$prefix" "$hash"
}

_image_inputs_impl() {
  local context_dir="$1" week="$2" repo_root context_path status dockerfile config_value config_status
  shift 2
  local -a raw_build_args=("$@") build_args=() tree_args=() words=() base_digests=()
  local line trimmed logical instruction ref image_ref alias digest_json digest mount
  local context_alias
  local stage_count=0 index from_ref source buildx_version builder_info buildkit_version driver
  local continuation=0 context_hash instruction_seen=0 pin_context
  declare -A digest_by_ref=() context_digest_by_ref=()
  _clear_image_inputs
  [[ "$week" =~ ^[0-9]{4}-W[0-9]{2}$ ]] || return 1
  [[ "${DOCKER_BUILDKIT:-}" != 0 ]] || return 1
  context_dir="$(cd "$context_dir" && pwd -P)" || return 1
  repo_root="$(git -C "$context_dir" rev-parse --show-toplevel 2>/dev/null)" || return 1
  context_path="${context_dir#"$repo_root"/}"
  [[ "$context_path" != "$context_dir" && -n "$context_path" ]] || return 1
  status="$(git -C "$repo_root" status --porcelain --ignored -- "$context_path" 2>/dev/null)" || return 1
  [[ -z "$status" ]] || return 1
  if config_value="$(git -C "$repo_root" config --get core.autocrlf 2>/dev/null)"; then
    [[ "$config_value" == false ]] || return 1
  else
    config_status=$?
    ((config_status == 1)) || return 1
  fi
  if config_value="$(git -C "$repo_root" config --get core.eol 2>/dev/null)"; then
    [[ "$config_value" == native ]] || return 1
  else
    config_status=$?
    ((config_status == 1)) || return 1
  fi
  if config_value="$(git -C "$repo_root" config --bool --get core.sparseCheckout 2>/dev/null)"; then
    [[ "$config_value" != true ]] || return 1
  else
    config_status=$?
    ((config_status == 1)) || return 1
  fi
  if config_value="$(git -C "$repo_root" config --bool --get core.symlinks 2>/dev/null)"; then
    [[ "$config_value" != false ]] || return 1
  else
    config_status=$?
    ((config_status == 1)) || return 1
  fi
  if config_value="$(git -C "$repo_root" config --bool --get core.fileMode 2>/dev/null)"; then
    [[ "$config_value" != false ]] || return 1
  else
    config_status=$?
    ((config_status == 1)) || return 1
  fi
  status="$(git -C "$repo_root" ls-files -v -- "$context_path" 2>/dev/null)" || return 1
  while IFS= read -r line; do
    [[ "$line" =~ ^[a-z] || "${line:0:1}" == S ]] && return 1
  done <<<"$status"
  context_hash="$(git -C "$repo_root" rev-parse "HEAD:$context_path" 2>/dev/null)" || return 1
  _hash_context_attributes "$repo_root" "$context_path" || return 1
  dockerfile="$context_dir/Dockerfile"
  [[ -f "$dockerfile" ]] || return 1

  for ((index = 0; index < ${#raw_build_args[@]}; index++)); do
    line="${raw_build_args[index]}"
    case "$line" in
      --build-arg)
        index=$((index + 1))
        ((index < ${#raw_build_args[@]})) || return 1
        line="${raw_build_args[index]}"
        ;;
      --build-arg=*) line="${line#--build-arg=}" ;;
    esac
    [[ "$line" == *=* && "${line%%=*}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    build_args+=("$line")
    [[ "$line" == GIT_SHA=* ]] || tree_args+=("$line")
  done

  logical=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    trimmed="${line%$'\r'}"
    trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ -z "$trimmed" ]] && continue
    if [[ "$trimmed" == \#* ]]; then
      if ((instruction_seen == 0)) && [[ "$trimmed" =~ ^#[[:space:]]*[[:alpha:]]+[[:space:]]*= ]]; then
        return 1
      fi
      continue
    fi
    instruction_seen=1
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
    instruction="${words[0]:-}"
    instruction="${instruction,,}"
    if [[ "$logical" == *'<<'* &&
      ( "$instruction" == run || "$instruction" == copy ) ]]; then
      return 1
    fi
    logical=""
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
          if [[ "$instruction" == run ]]; then
            mount="${words[index]#--mount=}"
            [[ ",$mount," != *,type=secret,* && ",$mount," != *,type=ssh,* ]] ||
              return 1
          fi
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
          pin_context="docker-image://$image_ref@$digest"
          if [[ -n "${context_digest_by_ref[$image_ref]:-}" &&
            "${context_digest_by_ref[$image_ref]}" != "$digest" ]]; then
            return 1
          fi
          context_digest_by_ref["$image_ref"]="$digest"
          IMAGE_BUILD_CONTEXT_ARGS+=("--build-context" "$image_ref=$pin_context")
          if [[ "$image_ref" == docker.io/* ]]; then
            context_alias="${image_ref#docker.io/}"
            context_alias="${context_alias#library/}"
            if [[ "$context_alias" != "$image_ref" ]]; then
              if [[ -n "${context_digest_by_ref[$context_alias]:-}" &&
                "${context_digest_by_ref[$context_alias]}" != "$digest" ]]; then
                return 1
              fi
              context_digest_by_ref["$context_alias"]="$digest"
              IMAGE_BUILD_CONTEXT_ARGS+=("--build-context" "$context_alias=$pin_context")
            fi
          fi
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
  driver="$(awk '/Driver:/ {print $2}' <<<"$builder_info" | LC_ALL=C sort -u | tr '\n' ',')"
  driver="${driver%,}"
  [[ -n "$driver" ]] || return 1
  IMAGE_KEY_BUILD_DRIVER="$driver"
  buildkit_version="$(awk '/BuildKit version:/ {print $3}' <<<"$builder_info" | LC_ALL=C sort -u | tr '\n' ',')"
  buildkit_version="${buildkit_version%,}"
  [[ -n "$buildkit_version" ]] || return 1
  IMAGE_KNOWN_STAGES=""

  IMAGE_KEY_CTX="$(_hash_image_key ctx "$context_hash" "$buildx_version" "$buildkit_version" \
    "$week" "${build_args[@]+"${build_args[@]}"}")" || return 1
  IMAGE_KEY_TREE="$(_hash_image_key tree "$context_hash" "$buildx_version" "$buildkit_version" \
    "$week" "${tree_args[@]+"${tree_args[@]}"}")" || return 1
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
  local metadata_file="$1" pinned_json non_image_prefixes='["file://"]'
  # BuildKit's local context is non-image input; its bytes are hashed separately.
  [[ -s "$metadata_file" && ${#IMAGE_PINNED_DIGESTS[@]} -gt 0 ]] || return 1
  pinned_json="$(printf '%s\n' "${IMAGE_PINNED_DIGESTS[@]+"${IMAGE_PINNED_DIGESTS[@]}"}" |
    jq -R 'sub("^sha256:"; "")' | jq -s .)" || return 1
  jq -e --argjson pinned "$pinned_json" --argjson non_image "$non_image_prefixes" '
    (.["buildx.build.provenance"].materials // []) as $materials
    | ($materials | type == "array")
      and all($materials[];
        . as $material
        | ($material.uri // "") as $uri
        | if ($uri | startswith("pkg:docker/")) then
          any($pinned[]; . == ($material.digest.sha256 // ""))
          else
            any($non_image[]; . as $prefix | $uri | startswith($prefix))
          end
      )
      and all($pinned[]; . as $digest | any($materials[]; .digest.sha256 == $digest))
  ' "$metadata_file" >/dev/null 2>&1
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
  local repo="$1" prefix="$2" token="" response page next all_items item pushed epoch
  local fraction fraction_text best_epoch=-1 best_fraction=-1 best_tag=""
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
  all_items="$(printf '%s\n' "${pages[@]+"${pages[@]}"}" | jq -s -c 'add')" || return 0
  while IFS= read -r item; do
    pushed="$(jq -er '.pushedAt | select(type == "string")' <<<"$item" 2>/dev/null)" || return 0
    epoch="$(date_epoch "$pushed")" || return 0
    fraction=0
    if [[ "$pushed" =~ \.([0-9]+) ]]; then
      fraction_text="${BASH_REMATCH[1]}000000000"
      fraction=$((10#${fraction_text:0:9}))
    fi
    if ((epoch > best_epoch || (epoch == best_epoch && fraction > best_fraction))); then
      best_epoch="$epoch"
      best_fraction="$fraction"
      best_tag="$(jq -er '.tag | select(type == "string")' <<<"$item" 2>/dev/null)" || return 0
    fi
  done < <(jq -c '.[]' <<<"$all_items")
  printf '%s\n' "$best_tag"
}
