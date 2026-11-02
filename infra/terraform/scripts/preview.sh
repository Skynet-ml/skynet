#!/usr/bin/env bash
# PR preview environments (SKY-164). .github/workflows/preview.yml and
# preview-cleanup.yml run this script; an operator runs setup-db once.
#
#   preview.sh claim <pr> <sha>    take a slot (exit 3 when all are taken)
#   preview.sh build <pr> <sha>    build and push the preview images, and migrate
#   preview.sh deploy <pr> <sha>   create or update the services
#   preview.sh staging-backend <pr> <sha>
#                                  print the staging backend image if backend/ matches
#   preview.sh delete <pr>         delete the preview (not its task definitions)
#   preview.sh list                print "<pr> <sha> <last-push-epoch>" per slot
#   preview.sh revision-prs        print the PRs that have preview task definitions
#   preview.sh prune <pr>          delete the task definitions of a PR without a slot
#   preview.sh setup-db            once, as an operator (skynet profile)
#
# A preview runs in the staging cluster with the staging secrets, task roles
# and security groups. It shares the staging ALB, RDS, Valkey and LiteLLM:
#   https://pr-<n>.<domain>      frontend   (the slot's frontend target group)
#   https://api-pr-<n>.<domain>  backend    (the slot's backend target group)
#   services pr-<n>-backend, pr-<n>-worker, pr-<n>-frontend (on-demand FARGATE)
#   database skynet_pr_<n> (copied from skynet_preview_template), Redis DB <slot>
#
# Slots: SSM parameters /skynet/preview/slots/1..4. A PR claims a free slot
# with PutParameter without overwrite, so two PRs never get the same slot.
# The slot picks the Redis DB index and the target groups and listener rules
# that Terraform made for it (modules/environment/preview.tf), and it holds
# the PR's last push time for the 48-hour cleanup. deploy points the slot's
# rules at the PR's hosts; delete points them back at a placeholder host.
#
# delete leaves the task definitions ACTIVE: the preview role cannot
# deregister revisions. prune runs from the scheduled cleanup on main, with
# its own role, and removes them.
#
# A PR whose backend/ tree equals the one of the staging backend image runs
# that image: the workflow sets BACKEND_IMAGE (from staging-backend) for
# build and deploy, and build does not build a backend image.
#
# The tasks get the staging REMOTE_DB_URL and REDIS_URL. A wrapper command
# points them at the preview database and Redis DB before the app starts, so
# no secret passes through this script or the task definition.

source "$(dirname "$0")/lib.sh"
need aws jq

env=staging
max_slots=4
slot_prefix=/skynet/preview/slots
repo_root="$(cd "$(dirname "$0")/../../.." && pwd)"

cmd="${1:-}"
pr="${2:-}"
sha="${3:-}"

check_pr() {
  [[ "$pr" =~ ^[1-9][0-9]{0,5}$ ]] || die "PR number must be a positive integer"
}

check_sha() {
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "third argument must be a full git SHA"
}

preview_config() {
  aws ssm get-parameter --name "/skynet/$env/preview" --query Parameter.Value --output text
}

# Prints one JSON object per held slot: {"slot":k,"pr":n,"sha":"...","at":epoch}.
slots() {
  local names=() k
  for ((k = 1; k <= max_slots; k++)); do names+=("$slot_prefix/$k"); done
  aws ssm get-parameters --names "${names[@]}" --query 'Parameters[].[Name,Value]' --output json |
    jq -c '.[] | (.[1] | fromjson) + {slot: (.[0] | split("/") | last | tonumber)}'
}

slot_of() {
  slots | jq -r --argjson pr "$1" 'select(.pr == $pr) | .slot' | sed -n 1p
}

slot_value() {
  jq -cn --argjson pr "$pr" --arg sha "$sha" --argjson at "$(date +%s)" '{pr: $pr, sha: $sha, at: $at}'
}

claim() {
  check_pr
  check_sha
  local k err
  k="$(slot_of "$pr")"
  if [[ -n "$k" ]]; then
    aws ssm put-parameter --name "$slot_prefix/$k" --type String --overwrite --value "$(slot_value)" >/dev/null
    echo "PR $pr keeps slot $k"
    return 0
  fi
  for ((k = 1; k <= max_slots; k++)); do
    if err="$(aws ssm put-parameter --name "$slot_prefix/$k" --type String --value "$(slot_value)" 2>&1 >/dev/null)"; then
      echo "PR $pr took slot $k"
      return 0
    fi
    [[ "$err" == *ParameterAlreadyExists* ]] || die "cannot claim slot $k: $err"
  done
  echo "all $max_slots preview slots are taken"
  exit 3
}

ecr_uri() {
  aws ecr describe-repositories --repository-names "skynet/preview-$1" \
    --query 'repositories[0].repositoryUri' --output text
}

# Succeeds when the image exists and is fresh. An image older than 11 days is
# deleted (the repositories are immutable), so build pushes it again: the ECR
# rule expires PR images 14 days after the push, and a preview may run up to
# 48 hours after its last deploy. The tag is deleted before the rebuild, so a
# failed rebuild leaves the preview unable to replace tasks until the next good
# build or the cleanup. Args: repository tag
image_is_fresh() {
  local pushed pushed_at err
  err="$(mktemp)"
  # stderr goes to a file, so stdout holds only the timestamp.
  if ! pushed="$(aws ecr describe-images --repository-name "$1" --image-ids "imageTag=$2" \
    --query 'imageDetails[0].imagePushedAt' --output text 2>"$err")"; then
    pushed="$(cat "$err")"
    rm -f "$err"
    [[ "$pushed" == *ImageNotFoundException* ]] && return 1
    die "describe-images $1:$2: $pushed"
  fi
  rm -f "$err"
  pushed_at="$(date -d "$pushed" +%s)" || die "cannot parse imagePushedAt: $pushed"
  if (($(date +%s) - pushed_at < 11 * 86400)); then
    return 0
  fi
  echo "deleting $1:$2: pushed at $pushed"
  aws ecr batch-delete-image --repository-name "$1" --image-ids "imageTag=$2" >/dev/null ||
    die "cannot delete $1:$2"
  return 1
}

# Builds and pushes the backend image unless a fresh one exists. Args: uri tag
build_backend() {
  if image_is_fresh skynet/preview-backend "$2"; then
    echo "skip: $1:$2 exists"
    return 0
  fi
  docker buildx build --platform linux/arm64 --provenance=false --progress=plain \
    -t "$1:$2" --push --build-arg "GIT_SHA=$sha" "$repo_root/backend"
}

# Next.js bakes NEXT_PUBLIC_* into the bundle, so each PR gets its own build.
# In GitHub Actions the layers go to the Actions cache, so a later push reuses
# the npm ci layer. Only the frontend uses it: the backend builds faster than
# the frontend at the same time, and the repository cache is near its limit.
# Args: uri tag domain
build_frontend() {
  local cache=()
  if image_is_fresh skynet/preview-frontend "$2"; then
    echo "skip: $1:$2 exists"
    return 0
  fi
  if [[ -n "${ACTIONS_RUNTIME_TOKEN:-}" && -n "${ACTIONS_RESULTS_URL:-}" ]]; then
    # mode=max: the npm ci layer is in the deps stage, not in the final image.
    cache=(--cache-from "type=gha,scope=preview-frontend"
      --cache-to "type=gha,mode=max,scope=preview-frontend,ignore-error=true")
  fi
  docker buildx build --platform linux/arm64 --provenance=false --progress=plain \
    -t "$1:$2" --push ${cache[@]+"${cache[@]}"} \
    --build-arg "NEXT_PUBLIC_SITE_URL=https://pr-$pr.$3" \
    --build-arg "NEXT_PUBLIC_SENTRY_DSN=${NEXT_PUBLIC_SENTRY_DSN:-}" \
    --build-arg "NEXT_PUBLIC_SENTRY_ENVIRONMENT=skynet-pr-$pr" \
    --build-arg "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE=${NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE:-}" \
    --build-arg "NEXT_PUBLIC_FEATURE_GENERALIST_AGENT=${NEXT_PUBLIC_FEATURE_GENERALIST_AGENT:-}" \
    --build-arg "APP_GIT_SHA=$sha" \
    --build-arg "APP_ENVIRONMENT=skynet-pr-$pr" \
    "$repo_root/frontend"
}

# Migrates the preview database with the backend image. Args: image cfg
migrate() {
  local slot td
  slot="$(slot_of "$pr")"
  [[ -n "$slot" ]] || die "PR $pr holds no preview slot; run claim first"
  td="$(register migrate "$1" "$(preview_env backend "$(jq -r .domain <<<"$2")")" "$slot" "$2" "$(account)")"
  echo "creating the database if needed, then migrating"
  run_migrate_task "$2" "$td"
  echo "migration ok"
}

# Builds the backend image (not when the preview runs the staging image),
# then migrates with it. Args: image cfg
backend_and_migrate() {
  if [[ -n "${BACKEND_IMAGE:-}" ]]; then
    echo "skip: the preview runs the staging backend image $1"
  else
    build_backend "${1%:*}" "${1##*:}" || return 1
  fi
  migrate "$1" "$2"
}

build() {
  check_pr
  check_sha
  need docker
  local cfg domain tag backend frontend backend_pid frontend_pid failed=""
  cfg="$(preview_config)"
  domain="$(jq -r .domain <<<"$cfg")"
  tag="$sha-pr-$pr"
  backend="${BACKEND_IMAGE:-$(ecr_uri backend):$tag}"
  frontend="$(ecr_uri frontend)"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${frontend%%/*}" >/dev/null

  # The backend side and the frontend build run at the same time. The
  # migration needs only the backend image, so it runs while the frontend
  # still builds. Each output line starts with its side.
  backend_and_migrate "$backend" "$cfg" 2>&1 | sed -u 's/^/[backend] /' &
  backend_pid=$!
  build_frontend "$frontend" "$tag" "$domain" 2>&1 | sed -u 's/^/[frontend] /' &
  frontend_pid=$!
  wait "$backend_pid" || failed+=" backend"
  wait "$frontend_pid" || failed+=" frontend"
  [[ -z "$failed" ]] || die "image build or migration failed:$failed"
  echo "ready: ${backend##*/} preview-frontend:$tag, database migrated"
}

# Runs before the app in the backend, worker and migrate containers. The
# shape checks stop a preview from ever using the staging database or Redis
# DB 0 if the generated URLs change form.
read -r -d '' url_wrapper <<'SH' || true
set -eu
case "$REMOTE_DB_URL" in */skynet\?*) ;; *) echo "preview: unexpected REMOTE_DB_URL form" >&2; exit 1 ;; esac
case "$REDIS_URL" in */0) ;; *) echo "preview: unexpected REDIS_URL form" >&2; exit 1 ;; esac
case "$PREVIEW_REDIS_DB" in [1-9]) ;; *) echo "preview: bad PREVIEW_REDIS_DB" >&2; exit 1 ;; esac
REMOTE_DB_URL="${REMOTE_DB_URL%/skynet\?*}/$PREVIEW_DB?${REMOTE_DB_URL##*\?}"
REDIS_URL="${REDIS_URL%/0}/$PREVIEW_REDIS_DB"
export REMOTE_DB_URL REDIS_URL
exec "$@"
SH

# The migrate container first creates the preview database, connected to the
# staging database as skynet_app (setup-db gave it CREATEDB). The connection
# limit keeps a preview from using up the shared RDS connections.
read -r -d '' create_db <<'SH' || true
set -eu
python - <<'PY'
import os

import psycopg2
from psycopg2 import sql

name = os.environ["PREVIEW_DB"]
conn = psycopg2.connect(os.environ["REMOTE_DB_URL"])
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
if cur.fetchone() is None:
    cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE skynet_preview_template CONNECTION LIMIT 20").format(sql.Identifier(name)))
    cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
    print(f"created database {name}")
PY
SH

# Delete: drop the database and empty the PR's Redis DB. It runs with the
# raw staging URLs, so it checks the Redis DB index itself.
read -r -d '' drop_db <<'SH' || true
set -eu
python - <<'PY'
import os

import psycopg2
import redis
from psycopg2 import sql

name = os.environ["PREVIEW_DB"]
slot = int(os.environ["PREVIEW_REDIS_DB"])
conn = psycopg2.connect(os.environ["REMOTE_DB_URL"])
conn.autocommit = True
conn.cursor().execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
print(f"dropped database {name}")
url = os.environ["REDIS_URL"]
if not 1 <= slot <= 9 or not url.endswith("/0"):
    raise SystemExit("preview: refusing to flush this Redis DB")
redis.Redis.from_url(url.removesuffix("/0") + f"/{slot}").flushdb()
print(f"flushed Redis DB {slot}")
PY
SH

account() {
  aws sts get-caller-identity --query Account --output text
}

# Prints the newest ACTIVE staging revision of a family whose roles are the
# staging roles for that family and whose tag is Environment=staging (the
# same rule as deploy.sh). The CLI pages through every ACTIVE revision.
staging_revision() {
  local key="$1" family="$2" acct="$3" arn roles want
  want="arn:aws:iam::$acct:role/skynet-$env-$key-exec"$'\t'"arn:aws:iam::$acct:role/skynet-$env-$key-task"$'\t'"$env"
  for arn in $(aws ecs list-task-definitions --family-prefix "$family" --status ACTIVE \
    --sort DESC --query 'taskDefinitionArns[]' --output text); do
    [[ "${arn##*/}" == "$family:"* ]] || continue
    roles="$(aws ecs describe-task-definition --task-definition "$arn" --include TAGS \
      --query "[taskDefinition.executionRoleArn, taskDefinition.taskRoleArn, tags[?key=='Environment'].value | [0]]" --output text)"
    if [[ "$roles" == "$want" ]]; then
      echo "$arn"
      return 0
    fi
  done
  die "no ACTIVE revision of $family has the $env roles and tag"
}

# Registers skynet-pr-<n>-<key> from the staging revision and prints its ARN.
# Args: key image env-json slot cfg acct
register() {
  local key="$1" image="$2" env_json="$3" slot="$4" cfg="$5" acct="$6" src td args
  src="$(staging_revision "$key" "$(jq -r --arg k "$key" '.families[$k]' <<<"$cfg")" "$acct")"
  td="$(aws ecs describe-task-definition --task-definition "$src" --query taskDefinition --output json)"
  args='null'
  if [[ "$key" != frontend ]]; then
    # The wrapper runs the command the container would run without it.
    args="$(jq -c '.containerDefinitions[0].command // empty' <<<"$td")"
    [[ -n "$args" ]] || args="$(sed -n 's/^CMD //p' "$repo_root/backend/Dockerfile" | tail -n1 | jq -c .)"
    if [[ "$key" == migrate ]]; then
      args="$(jq -c --arg w "$create_db"$'\n'"$url_wrapper" '["sh", "-c", $w, "preview"] + .' <<<"$args")"
    else
      args="$(jq -c --arg w "$url_wrapper" '["sh", "-c", $w, "preview"] + .' <<<"$args")"
    fi
    env_json="$(jq -c --arg db "skynet_pr_$pr" --arg slot "$slot" '. + {PREVIEW_DB: $db, PREVIEW_REDIS_DB: $slot}' <<<"$env_json")"
  fi
  td="$(jq --arg family "skynet-pr-$pr-$key" --arg img "$image" --argjson env "$env_json" --argjson cmd "$args" \
    --arg group "$(jq -r .log_group <<<"$cfg")" --arg prefix "pr-$pr" '
      .family = $family
      | .containerDefinitions[0].image = $img
      | .containerDefinitions[0].environment =
          ([.containerDefinitions[0].environment[] | select(.name as $n | $env | has($n) | not)]
           + ($env | to_entries | map({name: .key, value: .value})))
      | .containerDefinitions[0].secrets |= map(select(.name as $n | $env | has($n) | not))
      | .containerDefinitions[0].logConfiguration.options["awslogs-group"] = $group
      | .containerDefinitions[0].logConfiguration.options["awslogs-stream-prefix"] = $prefix
      | (if $cmd == null then . else .containerDefinitions[0].command = $cmd end)
      | .tags = [{key: "Environment", value: "preview"}, {key: "Project", value: "skynet"}, {key: "ManagedBy", value: "preview.sh"}]
      | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
            .registeredAt, .registeredBy, .deregisteredAt)' <<<"$td")"
  td="$(aws ecs register-task-definition --cli-input-json "$td" \
    --query taskDefinition.taskDefinitionArn --output text)"
  echo "registered $td" >&2
  echo "$td"
}

network_config() {
  local cfg="$1" key="$2"
  jq -r --arg k "$key" '"awsvpcConfiguration={subnets=[\(.subnets | join(","))],securityGroups=[\(.security_groups[$k])],assignPublicIp=\(if .assign_public_ip then "ENABLED" else "DISABLED" end)}"' <<<"$cfg"
}

wait_task() {
  local cluster="$1" arn="$2" i status code reason
  for ((i = 0; i < 180; i++)); do
    status="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$arn" --query 'tasks[0].lastStatus' --output text)"
    [[ "$status" == "STOPPED" ]] && break
    sleep 10
  done
  [[ "$status" == "STOPPED" ]] || die "task $arn still $status after 30 minutes"
  code="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$arn" --query 'tasks[0].containers[0].exitCode' --output text)"
  reason="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$arn" --query 'tasks[0].stoppedReason' --output text)"
  [[ "$code" == "0" ]] || die "task $arn failed (exit=$code, reason=$reason). Logs: CloudWatch /ecs/skynet-$env/preview, streams pr-$pr/*"
}

run_migrate_task() {
  local cfg="$1" td="$2" overrides="${3:-}" cluster arn args
  cluster="$(jq -r .cluster <<<"$cfg")"
  args=(
    --cluster "$cluster" --task-definition "$td" --launch-type FARGATE
    --network-configuration "$(network_config "$cfg" migrate)"
    --query 'tasks[0].taskArn' --output text
  )
  [[ -z "$overrides" ]] || args+=(--overrides "$overrides")
  arn="$(aws ecs run-task "${args[@]}")"
  [[ "$arn" == arn:* ]] || die "run-task did not start a task"
  wait_task "$cluster" "$arn"
}

# Prints a field of the slot's target group and rule for a service.
# Args: cfg slot key field (target_group, rule or placeholder)
slot_field() {
  jq -er --arg s "$2" --arg k "$3" --arg f "$4" '.slots[$s][$k][$f]' <<<"$1"
}

# Points a slot rule at a host. The rule always forwards to the slot's group.
# Args: rule-arn host target-group-arn
route() {
  aws elbv2 modify-rule --rule-arn "$1" --conditions "Field=host-header,Values=$2" \
    --actions "Type=forward,TargetGroupArn=$3" >/dev/null
  echo "routing $2"
}

rule_host() {
  aws elbv2 describe-rules --rule-arns "$1" \
    --query "Rules[0].Conditions[?Field=='host-header'] | [0].Values[0]" --output text
}

service_active() {
  [[ "$(aws ecs describe-services --cluster "$1" --services "$2" \
    --query 'services[0].status' --output text 2>/dev/null)" == "ACTIVE" ]]
}

# Args: cfg key td [target-group-arn port]
upsert_service() {
  local cfg="$1" key="$2" td="$3" tg="${4:-}" port="${5:-}" cluster name args
  cluster="$(jq -r .cluster <<<"$cfg")"
  name="pr-$pr-$key"
  if service_active "$cluster" "$name"; then
    aws ecs update-service --cluster "$cluster" --service "$name" --task-definition "$td" >/dev/null
    echo "updating $name"
    return 0
  fi
  args=(
    --cluster "$cluster" --service-name "$name" --task-definition "$td" --desired-count 1
    --capacity-provider-strategy "capacityProvider=FARGATE,weight=1" --platform-version LATEST
    --network-configuration "$(network_config "$cfg" "$key")"
    --deployment-configuration "deploymentCircuitBreaker={enable=true,rollback=true},maximumPercent=200,minimumHealthyPercent=100"
    --propagate-tags SERVICE
    --tags "key=Environment,value=preview" "key=Project,value=skynet" "key=ManagedBy,value=preview.sh"
  )
  if [[ -n "$tg" ]]; then
    args+=(--load-balancers "targetGroupArn=$tg,containerName=$key,containerPort=$port" --health-check-grace-period-seconds 180)
  fi
  aws ecs create-service "${args[@]}" >/dev/null
  echo "creating $name"
}

# Waits until the newest deployment of each service runs. A deployment runs
# when ECS marks it COMPLETED, or when it runs the desired count of tasks
# with no failed task for four checks in a row (30 seconds or more). ECS
# marks a deployment COMPLETED only after the old tasks drain and stop, and
# then only some minutes later for the worker, which has no health check.
# The workflow's next step checks that the backend and the frontend serve the
# commit through the ALB. Fails at once when ECS marks one FAILED (the
# circuit breaker gave up).
wait_services() {
  local cluster="$1" i states up=0
  shift
  for ((i = 0; i < 180; i++)); do
    states="$(aws ecs describe-services --cluster "$cluster" --services "$@" --output json |
      jq -r '[.services[] | (.deployments[] | select(.status == "PRIMARY")
        | if .rolloutState == "IN_PROGRESS" and .runningCount >= .desiredCount and .failedTasks == 0
          then "RUNNING" else .rolloutState end)] | join(" ")')"
    [[ "$states" == *FAILED* ]] && die "a preview service failed to start ($states). See the ECS service events."
    [[ "$states" =~ ^(COMPLETED ?)+$ ]] && return 0
    if [[ "$states" =~ ^((COMPLETED|RUNNING) ?)+$ ]]; then
      if ((++up >= 4)); then
        return 0
      fi
    else
      up=0
    fi
    sleep 10
  done
  die "preview services not running after 30 minutes ($states)"
}

# Prints the environment of the backend (and worker and migrate) or the
# frontend as JSON. Args: backend|frontend domain
preview_env() {
  local app="https://pr-$pr.$2" api="https://api-pr-$pr.$2" e="skynet-pr-$pr"
  if [[ "$1" == backend ]]; then
    # The remote MCP server (SKY-229) of a preview lives under its API host,
    # because the *.<domain> wildcard certificate has no mcp-pr-<n> host.
    jq -cn --arg e "$e" --arg sha "$sha" --arg app "$app" --arg mcp "$api/remote-mcp" '{
      APP_ENVIRONMENT: $e, APP_GIT_SHA: $sha, SENTRY_ENVIRONMENT: $e, ALERT_ENVIRONMENT: $e,
      ALLOWED_ORIGINS: $app, APP_PUBLIC_URL: $app, FRONTEND_URL: $app, MCP_RESOURCE_URL: $mcp,
      DB_POOL_SIZE: "3", DB_POOL_MAX_OVERFLOW: "2"}'
  else
    # Google and GitHub know only staging's callback URL; staging forwards it here.
    jq -cn --arg e "$e" --arg sha "$sha" --arg app "$app" --arg api "$api" \
      --arg proxy "https://$2/api/auth" '{
      APP_ENVIRONMENT: $e, APP_GIT_SHA: $sha, SENTRY_ENVIRONMENT: $e, NEXT_PUBLIC_SENTRY_ENVIRONMENT: $e,
      API_URL: $api, NEXT_PUBLIC_API_URL: $api, NEXT_PUBLIC_SITE_URL: $app, AUTH_URL: $app,
      AUTH_REDIRECT_PROXY_URL: $proxy}'
  fi
}

# The migration ran in build, while the frontend image built.
deploy() {
  check_pr
  check_sha
  local cfg slot acct domain app api tag backend_img frontend_img backend_env
  local cluster tg_backend tg_frontend
  slot="$(slot_of "$pr")"
  [[ -n "$slot" ]] || die "PR $pr holds no preview slot; run claim first"
  cfg="$(preview_config)"
  acct="$(account)"
  domain="$(jq -r .domain <<<"$cfg")"
  app="pr-$pr.$domain"
  api="api-pr-$pr.$domain"
  tag="$sha-pr-$pr"
  backend_img="${BACKEND_IMAGE:-$(ecr_uri backend):$tag}"
  frontend_img="$(ecr_uri frontend):$tag"
  backend_env="$(preview_env backend "$domain")"

  cluster="$(jq -r .cluster <<<"$cfg")"
  tg_backend="$(slot_field "$cfg" "$slot" backend target_group)"
  tg_frontend="$(slot_field "$cfg" "$slot" frontend target_group)"
  route "$(slot_field "$cfg" "$slot" backend rule)" "$api" "$tg_backend"
  route "$(slot_field "$cfg" "$slot" frontend rule)" "$app" "$tg_frontend"

  upsert_service "$cfg" backend \
    "$(register backend "$backend_img" "$(jq -c --arg app "$app" '. + {WEBAUTHN_RP_ID: $app, WEBAUTHN_ORIGINS: .ALLOWED_ORIGINS}' <<<"$backend_env")" "$slot" "$cfg" "$acct")" \
    "$tg_backend" "$(jq -r .ports.backend <<<"$cfg")"
  upsert_service "$cfg" worker "$(register worker "$backend_img" "$backend_env" "$slot" "$cfg" "$acct")"
  upsert_service "$cfg" frontend "$(register frontend "$frontend_img" "$(preview_env frontend "$domain")" "$slot" "$cfg" "$acct")" \
    "$tg_frontend" "$(jq -r .ports.frontend <<<"$cfg")"

  echo "waiting for the preview services"
  wait_services "$cluster" "pr-$pr-backend" "pr-$pr-worker" "pr-$pr-frontend"
  echo "preview of PR $pr runs: https://$app (API https://$api)"
}

# Prints the image of the newest staging backend revision when it was built
# from the same backend/ tree as <sha>; prints nothing otherwise. Then the
# preview can run that image: same code, same migrations. The image tag is
# the staging commit. Needs a git checkout that can fetch that commit.
staging_backend() {
  check_pr
  check_sha
  local cfg img commit
  cfg="$(preview_config)"
  img="$(aws ecs describe-task-definition \
    --task-definition "$(staging_revision backend "$(jq -r .families.backend <<<"$cfg")" "$(account)")" \
    --query 'taskDefinition.containerDefinitions[0].image' --output text)"
  commit="${img##*:}"
  # Any doubt prints nothing, so the preview builds its own backend image.
  if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
    echo "the staging backend image tag is not a commit: $img; the preview builds its own backend image" >&2
  elif ! git -C "$repo_root" cat-file -e "$commit^{commit}" 2>/dev/null &&
    ! git -C "$repo_root" fetch -q --depth=1 origin "$commit"; then
    echo "cannot fetch staging commit $commit; the preview builds its own backend image" >&2
  elif [[ "$(git -C "$repo_root" rev-parse "$sha:backend")" == "$(git -C "$repo_root" rev-parse "$commit:backend")" ]]; then
    echo "$img"
  else
    echo "backend/ at ${sha:0:8} differs from staging ${commit:0:8}; the preview builds its own backend image" >&2
  fi
}

delete() {
  check_pr
  local cfg cluster domain slot version s key host rule names=() acct src img migrate_td
  cfg="$(preview_config)"
  cluster="$(jq -r .cluster <<<"$cfg")"
  domain="$(jq -r .domain <<<"$cfg")"
  slot="$(slot_of "$pr")"
  # The slot is released at the end only if nothing wrote it in between.
  [[ -z "$slot" ]] || version="$(aws ssm get-parameter --name "$slot_prefix/$slot" --query Parameter.Version --output text)"

  for s in backend worker frontend; do
    if service_active "$cluster" "pr-$pr-$s"; then
      aws ecs delete-service --cluster "$cluster" --service "pr-$pr-$s" --force >/dev/null
      names+=("pr-$pr-$s")
      echo "deleting service pr-$pr-$s"
    fi
  done
  if ((${#names[@]} > 0)); then
    aws ecs wait services-inactive --cluster "$cluster" --services "${names[@]}"
  fi

  if [[ -n "$slot" ]]; then
    for key in backend frontend; do
      rule="$(slot_field "$cfg" "$slot" "$key" rule)"
      host="pr-$pr.$domain"
      [[ "$key" == frontend ]] || host="api-$host"
      if [[ "$(rule_host "$rule")" == "$host" ]]; then
        route "$rule" "$(slot_field "$cfg" "$slot" "$key" placeholder)" "$(slot_field "$cfg" "$slot" "$key" target_group)"
      fi
    done
  fi

  # Only while the PR holds its slot: a delete that released the slot already
  # dropped the database and flushed the Redis DB. The drop runs in a fresh
  # migrate revision with the staging image, because the PR's images can be
  # gone (an earlier cleanup, or the ECR lifecycle rule). It is idempotent.
  # The Redis DB comes from the slot, which this PR holds.
  if [[ -n "$slot" ]]; then
    acct="$(account)"
    src="$(staging_revision migrate "$(jq -r .families.migrate <<<"$cfg")" "$acct")"
    img="$(aws ecs describe-task-definition --task-definition "$src" \
      --query 'taskDefinition.containerDefinitions[0].image' --output text)"
    migrate_td="$(register migrate "$img" '{}' "$slot" "$cfg" "$acct")"
    echo "dropping database skynet_pr_$pr"
    run_migrate_task "$cfg" "$migrate_td" \
      "$(jq -cn --arg s "$drop_db" --arg slot "$slot" '{containerOverrides: [{name: "migrate", command: ["sh", "-c", $s],
        environment: [{name: "PREVIEW_REDIS_DB", value: $slot}]}]}')"
  fi

  # A failed delete keeps the slot and the next run retries. The images go
  # only after the release: a slot that changed belongs to a newer deploy of
  # the PR, which may use the same images. Images that stay behind expire
  # under the ECR lifecycle rule.
  if [[ -n "$slot" ]]; then
    if [[ "$(aws ssm get-parameter --name "$slot_prefix/$slot" --query Parameter.Version --output text)" != "$version" ]]; then
      echo "slot $slot changed during the delete; it stays held, and so do the images"
      return 0
    fi
    aws ssm delete-parameter --name "$slot_prefix/$slot"
    echo "released slot $slot"
  fi
  delete_images
  echo "preview of PR $pr deleted"
}

delete_images() {
  local repo tags
  for repo in skynet/preview-backend skynet/preview-frontend; do
    tags="$(aws ecr list-images --repository-name "$repo" --filter tagStatus=TAGGED --output json |
      jq -c --arg sfx "-pr-$pr" '[.imageIds[] | select(.imageTag | endswith($sfx)) | {imageTag}]')"
    while [[ "$(jq length <<<"$tags")" -gt 0 ]]; do
      aws ecr batch-delete-image --repository-name "$repo" --image-ids "$(jq -c '.[:100]' <<<"$tags")" >/dev/null
      tags="$(jq -c '.[100:]' <<<"$tags")"
    done
  done
  echo "deleted the images"
}

list() {
  slots | jq -r '"\(.pr) \(.sha) \(.at)"'
}

revision_prs() {
  aws ecs list-task-definition-families --family-prefix skynet-pr- --status ALL \
    --query 'families[]' --output text | tr '\t' '\n' |
    sed -nE 's/^skynet-pr-([1-9][0-9]*)-(backend|worker|frontend|migrate)$/\1/p' | sort -un
}

# Deregisters and deletes every revision of the PR's families. Runs with the
# cleanup role (main only). It checks the slot itself, right before, so a PR
# that took a slot again keeps its revisions.
prune() {
  check_pr
  local fam arn tds batch
  [[ -z "$(slot_of "$pr")" ]] || { echo "PR $pr holds a slot; keeping its task definitions"; return 0; }
  for fam in backend worker frontend migrate; do
    for arn in $(aws ecs list-task-definitions --family-prefix "skynet-pr-$pr-$fam" --status ACTIVE \
      --query 'taskDefinitionArns[]' --output text); do
      [[ "${arn##*/}" == "skynet-pr-$pr-$fam:"* ]] || continue
      aws ecs deregister-task-definition --task-definition "$arn" >/dev/null
    done
    tds="$(aws ecs list-task-definitions --family-prefix "skynet-pr-$pr-$fam" --status INACTIVE --output json |
      jq -c --arg f "skynet-pr-$pr-$fam:" '[.taskDefinitionArns[] | select(split("/") | last | startswith($f))]')"
    while [[ "$(jq length <<<"$tds")" -gt 0 ]]; do
      mapfile -t batch < <(jq -r '.[:10][]' <<<"$tds")
      aws ecs delete-task-definitions --task-definitions "${batch[@]}" >/dev/null
      tds="$(jq -c '.[10:]' <<<"$tds")"
    done
  done
  echo "deleted the task definitions of PR $pr"
}

# Once per environment, as an operator: lets skynet_app create the preview
# databases, and makes the template they copy (with pgvector, which only the
# master may create). Runs as the dbadmin task, like create-db-roles.sh.
setup_db() {
  local script overrides task
  read -r -d '' script <<'SH' || true
set -eu
psql -v ON_ERROR_STOP=1 <<'SQL'
ALTER ROLE skynet_app CREATEDB;
SELECT 'CREATE DATABASE skynet_preview_template OWNER skynet_app' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'skynet_preview_template') \gexec
REVOKE ALL ON DATABASE skynet_preview_template FROM PUBLIC;
SQL
psql -v ON_ERROR_STOP=1 -d skynet_preview_template -c 'CREATE EXTENSION IF NOT EXISTS vector'
psql -v ON_ERROR_STOP=1 -c 'ALTER DATABASE skynet_preview_template IS_TEMPLATE true'
echo "skynet_app may create databases; skynet_preview_template is ready"
SH
  overrides="$(jq -n --arg s "$script" '{containerOverrides: [{name: "dbadmin", command: ["sh", "-c", $s]}]}')"
  task="$(run_task "$env" dbadmin "$overrides")"
  echo "started $task"
  wait_task_ok "$env" "$task"
  echo "done. Output: CloudWatch log group /ecs/skynet-$env/dbadmin"
}

case "$cmd" in
  claim) claim ;;
  build) build ;;
  deploy) deploy ;;
  staging-backend) staging_backend ;;
  delete) delete ;;
  list) list ;;
  revision-prs) revision_prs ;;
  prune) prune ;;
  setup-db) setup_db ;;
  *) die "usage: preview.sh claim|build|deploy|staging-backend <pr> <sha> | delete|prune <pr> | list | revision-prs | setup-db" ;;
esac
