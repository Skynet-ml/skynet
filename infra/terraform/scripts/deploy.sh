#!/usr/bin/env bash
# Deploys one commit to one environment:
#   1. registers a new revision of every task family with the new image tag
#      (environment and secrets carry over from the latest revision),
#   2. runs `python manage.py migrate` as a one-off task and stops on failure,
#   3. updates the services and waits until they are stable.
#
#   infra/terraform/scripts/deploy.sh <staging|production> <git-sha> [--migrate-only|--skip-migrate]
#
# Build and push the images first (scripts/build-push.sh).
# Rollback: run this script again with the previous SHA. Migrations do not
# roll back; write them so the previous release still runs on the new schema.
#
# Environment isolation: a new revision starts from the newest revision of
# the family whose execution and task roles are this environment's own roles
# (skynet-<env>-<family>-exec/-task). The other environment's deploy role
# cannot pass these roles, so it cannot plant a revision this script uses.
# The revision must also carry the tag Environment=<env>: the PR preview role
# can pass the staging roles, but it can only register revisions of the
# skynet-pr-* families, tagged Environment=preview.
# New revisions carry the tag Environment=<env>; the deploy role must send it.

source "$(dirname "$0")/lib.sh"
need aws jq

env="${1:-}"
sha="${2:-}"
mode="${3:-}"
check_env "$env"
[[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || die "second argument must be a git SHA"
[[ -z "$mode" || "$mode" == --migrate-only || "$mode" == --skip-migrate ]] ||
  die "mode must be --migrate-only or --skip-migrate"
if [[ "$mode" == --skip-migrate ]]; then
  [[ "${MIGRATED_SHA:-}" == "$sha" ]] ||
    die "--skip-migrate requires MIGRATED_SHA to equal $sha"
fi

cfg="$(deploy_config "$env")"
cluster="$(jq -r .cluster <<<"$cfg")"
sfx="$(jq -r .frontend_tag_sfx <<<"$cfg")"

image_for() {
  case "$1" in
    backend | worker | migrate) echo "$(jq -r .ecr.backend <<<"$cfg"):$sha" ;;
    frontend) echo "$(jq -r .ecr.frontend <<<"$cfg"):$sha$sfx" ;;
    litellm) echo "$(jq -r .ecr.litellm <<<"$cfg"):$sha" ;;
  esac
}

repo_tags=("backend:$sha")
if [[ "$mode" != --migrate-only ]]; then
  repo_tags+=("litellm:$sha" "frontend:$sha$sfx")
fi
for repo_tag in "${repo_tags[@]}"; do
  aws ecr describe-images --repository-name "skynet/${repo_tag%%:*}" --image-ids "imageTag=${repo_tag#*:}" >/dev/null 2>&1 ||
    die "image skynet/$repo_tag is not in ECR. Run scripts/build-push.sh first."
done

account="$(aws sts get-caller-identity --query Account --output text)"

# Prints the newest ACTIVE revision of a family whose roles are this
# environment's roles for that family. The CLI pages through every ACTIVE
# revision.
trusted_revision() {
  local family_key="$1" family want_exec want_task arn roles
  family="$(jq -r --arg f "$family_key" '.families[$f]' <<<"$cfg")"
  want_exec="arn:aws:iam::$account:role/skynet-$env-$family_key-exec"
  want_task="arn:aws:iam::$account:role/skynet-$env-$family_key-task"
  for arn in $(aws ecs list-task-definitions --family-prefix "$family" --status ACTIVE \
    --sort DESC --query 'taskDefinitionArns[]' --output text); do
    [[ "${arn##*/}" == "$family:"* ]] || continue
    roles="$(aws ecs describe-task-definition --task-definition "$arn" --include TAGS \
      --query "[taskDefinition.executionRoleArn, taskDefinition.taskRoleArn, tags[?key=='Environment'].value | [0]]" --output text)"
    if [[ "$roles" == "$want_exec"$'\t'"$want_task"$'\t'"$env" ]]; then
      echo "$arn"
      return 0
    fi
    echo "skipping $arn: its roles are not skynet-$env-$family_key-exec/-task or its tag is not Environment=$env" >&2
  done
  die "no ACTIVE revision of $family has the $env roles and tag; run terraform apply for $env"
}

# Prints the new task definition ARN. Bash 5.1 or newer is required by lib.sh.
register() {
  local family_key="$1" image td src
  image="$(image_for "$family_key")"
  src="$(trusted_revision "$family_key")"
  td="$(aws ecs describe-task-definition --task-definition "$src" --query taskDefinition --output json)"
  td="$(jq --arg img "$image" --arg sha "$sha" --arg env "$env" '
      .containerDefinitions[0].image = $img
      | .containerDefinitions[0].environment |= map(if .name == "APP_GIT_SHA" then .value = $sha else . end)
      | .tags = [{key: "Environment", value: $env}, {key: "Project", value: "skynet"}, {key: "ManagedBy", value: "deploy.sh"}]
      | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
            .registeredAt, .registeredBy, .deregisteredAt)' <<<"$td")"
  td="$(aws ecs register-task-definition --cli-input-json "$td" \
    --query taskDefinition.taskDefinitionArn --output text)"
  echo "registered $td" >&2
  echo "$td"
}

if [[ "$mode" != "--skip-migrate" ]]; then
  migrate_td="$(register migrate)"
  echo "running migration"
  task="$(run_task "$env" migrate "" "$migrate_td")"
  wait_task_ok "$env" "$task"
  echo "migration ok"
fi

[[ "$mode" == "--migrate-only" ]] && exit 0

services=()
while IFS= read -r s; do services+=("$s"); done < <(jq -r '.services[]' <<<"$cfg")

for s in "${services[@]}"; do
  arn="$(register "$s")"
  aws ecs update-service --cluster "$cluster" --service "$s" \
    --task-definition "$arn" --query service.serviceName --output text >/dev/null
  echo "updating $s"
done

echo "waiting for services to become stable"
aws ecs wait services-stable --cluster "$cluster" --services "${services[@]}"
echo "deployed $sha to $env"
