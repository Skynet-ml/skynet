#!/usr/bin/env bash
# Proves that each GitHub deploy role cannot change the other environment,
# that the PR preview role reaches only preview resources, and that only main
# can assume the preview cleanup role (it may deregister any revision).
# It reads the rendered deploy policies from a global plan and tests them with
# the IAM policy simulator (read-only; it creates and changes nothing).
#
# Usage: scripts/check-deploy-isolation.sh <plan.json>
#   plan.json: `terraform -chdir=global show -json <planfile>` output.
#
# It also checks that ECS tag authorization is on, because the
# RegisterTaskDefinition rule depends on ECS authorizing tags on create.

source "$(dirname "$0")/lib.sh"
need aws jq

plan="${1:?usage: check-deploy-isolation.sh <plan.json>}"
account="$(aws sts get-caller-identity --query Account --output text)"
region="$AWS_REGION"
fails=0

# Arg: the address of a policy document data source in the global stack.
policy_for() {
  jq -er --arg a "$1" '
    [.prior_state.values.root_module.resources[]? | select(.address == $a) | .values.json][0]
    // error("no rendered policy for " + $a)' "$plan"
}

# Args: policy want(allowed|denied) label action resource [context-entries...]
expect() {
  local policy="$1" want="$2" label="$3" action="$4" resource="$5" got
  shift 5
  got="$(aws iam simulate-custom-policy --policy-input-list "$policy" \
    --action-names "$action" --resource-arns "$resource" "$@" \
    --query 'EvaluationResults[0].EvalDecision' --output text)"
  if [[ "$got" == "allowed" && "$want" == "allowed" ]] || [[ "$got" != "allowed" && "$want" == "denied" ]]; then
    echo "ok    $label ($got)"
  else
    echo "FAIL  $label: want $want, got $got"
    fails=$((fails + 1))
  fi
}

tag() { echo "ContextKeyName=aws:RequestTag/Environment,ContextKeyValues=$1,ContextKeyType=string"; }
pass_ecs="ContextKeyName=iam:PassedToService,ContextKeyValues=ecs-tasks.amazonaws.com,ContextKeyType=string"
cluster() { echo "ContextKeyName=ecs:cluster,ContextKeyValues=arn:aws:ecs:$region:$account:cluster/skynet-$1,ContextKeyType=string"; }

setting="$(aws ecs list-account-settings --name tagResourceAuthorization --effective-settings \
  --query 'settings[0].value' --output text)"
if [[ "$setting" == "on" ]]; then
  echo "ok    ECS tagResourceAuthorization is on"
else
  echo "FAIL  ECS tagResourceAuthorization is '$setting'; tag rules are not enforced"
  fails=$((fails + 1))
fi

for env in staging production; do
  other=production
  [[ "$env" == production ]] && other=staging
  policy="$(policy_for "data.aws_iam_policy_document.github_deploy[\"$env\"]")"
  td="arn:aws:ecs:$region:$account:task-definition"
  echo "-- deploy role for $env"
  expect "$policy" allowed "register a revision tagged $env" ecs:RegisterTaskDefinition "*" --context-entries "$(tag "$env")"
  expect "$policy" denied "register a revision tagged $other" ecs:RegisterTaskDefinition "*" --context-entries "$(tag "$other")"
  expect "$policy" denied "register a revision without tags" ecs:RegisterTaskDefinition "*"
  expect "$policy" allowed "tag a $env backend revision" ecs:TagResource "$td/skynet-$env-backend:999"
  expect "$policy" denied "tag a $other backend revision" ecs:TagResource "$td/skynet-$other-backend:999"
  expect "$policy" allowed "read the tags of a $env backend revision" ecs:ListTagsForResource "$td/skynet-$env-backend:999"
  expect "$policy" denied "read the tags of a $other backend revision" ecs:ListTagsForResource "$td/skynet-$other-backend:999"
  expect "$policy" allowed "pass the $env backend exec role" iam:PassRole "arn:aws:iam::$account:role/skynet-$env-backend-exec" --context-entries "$pass_ecs"
  expect "$policy" denied "pass the $other backend exec role" iam:PassRole "arn:aws:iam::$account:role/skynet-$other-backend-exec" --context-entries "$pass_ecs"
  expect "$policy" denied "pass the $other backend task role" iam:PassRole "arn:aws:iam::$account:role/skynet-$other-backend-task" --context-entries "$pass_ecs"
  expect "$policy" allowed "update a $env service" ecs:UpdateService "arn:aws:ecs:$region:$account:service/skynet-$env/skynet-$env-backend"
  expect "$policy" denied "update a $other service" ecs:UpdateService "arn:aws:ecs:$region:$account:service/skynet-$other/skynet-$other-backend"
  expect "$policy" allowed "run the $env migrate task" ecs:RunTask "$td/skynet-$env-migrate:999" --context-entries "$(cluster "$env")"
  expect "$policy" denied "run the $other migrate task" ecs:RunTask "$td/skynet-$other-migrate:999" --context-entries "$(cluster "$other")"
  expect "$policy" denied "read the $other deploy parameter" ssm:GetParameter "arn:aws:ssm:$region:$account:parameter/skynet/$other/deploy"
  expect "$policy" allowed "push to the $env build cache" ecr:PutImage \
    "arn:aws:ecr:$region:$account:repository/skynet/buildcache-$env"
  expect "$policy" denied "push to the $other build cache" ecr:PutImage \
    "arn:aws:ecr:$region:$account:repository/skynet/buildcache-$other"
  for action in ecr:BatchCheckLayerAvailability ecr:BatchGetImage ecr:DescribeImages \
    ecr:DescribeRepositories ecr:GetDownloadUrlForLayer; do
    expect "$policy" allowed "read the $env build cache with $action" "$action" \
      "arn:aws:ecr:$region:$account:repository/skynet/buildcache-$env"
    expect "$policy" denied "read the $other build cache with $action" "$action" \
      "arn:aws:ecr:$region:$account:repository/skynet/buildcache-$other"
    expect "$policy" denied "read the preview build cache with $action" "$action" \
      "arn:aws:ecr:$region:$account:repository/skynet/preview-buildcache"
  done
  for action in ecr:BatchDeleteImage ecr:CompleteLayerUpload ecr:DeleteRepository \
    ecr:InitiateLayerUpload ecr:PutImage ecr:PutImageTagMutability \
    ecr:PutLifecyclePolicy ecr:SetRepositoryPolicy ecr:UploadLayerPart; do
    expect "$policy" denied "write the preview build cache with $action" "$action" \
      "arn:aws:ecr:$region:$account:repository/skynet/preview-buildcache"
  done
done

policy="$(policy_for data.aws_iam_policy_document.github_preview)"
td="arn:aws:ecs:$region:$account:task-definition"
svc="arn:aws:ecs:$region:$account:service"
listener="arn:aws:elasticloadbalancing:$region:$account:listener/app/skynet-staging/0/0"
rule="arn:aws:elasticloadbalancing:$region:$account:listener-rule/app/skynet-staging/0/0/0"
task_def() { echo "ContextKeyName=ecs:task-definition,ContextKeyValues=$1,ContextKeyType=string"; }
resource_tag() { echo "ContextKeyName=aws:ResourceTag/Environment,ContextKeyValues=$1,ContextKeyType=string"; }
echo "-- preview role"
expect "$policy" allowed "register a preview revision tagged preview" ecs:RegisterTaskDefinition "$td/skynet-pr-1-backend:999" --context-entries "$(tag preview)"
for env in staging production; do
  expect "$policy" denied "register a preview revision tagged $env" ecs:RegisterTaskDefinition "$td/skynet-pr-1-backend:999" --context-entries "$(tag "$env")"
  expect "$policy" denied "register a $env backend revision tagged preview" ecs:RegisterTaskDefinition "$td/skynet-$env-backend:999" --context-entries "$(tag preview)"
  expect "$policy" denied "deregister a $env backend revision" ecs:DeregisterTaskDefinition "$td/skynet-$env-backend:999"
  expect "$policy" denied "tag a $env backend revision" ecs:TagResource "$td/skynet-$env-backend:999"
  expect "$policy" denied "delete a $env backend revision" ecs:DeleteTaskDefinitions "$td/skynet-$env-backend:999"
  expect "$policy" denied "pass the $env litellm task role" iam:PassRole "arn:aws:iam::$account:role/skynet-$env-litellm-task" --context-entries "$pass_ecs"
  expect "$policy" denied "pass the $env dbadmin exec role" iam:PassRole "arn:aws:iam::$account:role/skynet-$env-dbadmin-exec" --context-entries "$pass_ecs"
  expect "$policy" denied "update the $env backend service" ecs:UpdateService "$svc/skynet-$env/skynet-$env-backend"
  expect "$policy" denied "run the $env migrate task" ecs:RunTask "$td/skynet-$env-migrate:999" --context-entries "$(cluster "$env")"
  expect "$policy" denied "read the $env deploy parameter" ssm:GetParameter "arn:aws:ssm:$region:$account:parameter/skynet/$env/deploy"
done
expect "$policy" denied "register a preview revision without tags" ecs:RegisterTaskDefinition "$td/skynet-pr-1-backend:999"
expect "$policy" allowed "tag a preview revision" ecs:TagResource "$td/skynet-pr-1-backend:999"
expect "$policy" denied "deregister a preview revision" ecs:DeregisterTaskDefinition "$td/skynet-pr-1-backend:999"
expect "$policy" denied "delete a preview revision" ecs:DeleteTaskDefinitions "$td/skynet-pr-1-backend:999"
expect "$policy" allowed "pass the staging backend exec role" iam:PassRole "arn:aws:iam::$account:role/skynet-staging-backend-exec" --context-entries "$pass_ecs"
expect "$policy" denied "pass the production backend task role" iam:PassRole "arn:aws:iam::$account:role/skynet-production-backend-task" --context-entries "$pass_ecs"
expect "$policy" allowed "update a preview service" ecs:UpdateService "$svc/skynet-staging/pr-1-backend" --context-entries "$(task_def "$td/skynet-pr-1-backend:999")"
expect "$policy" denied "run the staging dbadmin revision in a preview service" ecs:UpdateService "$svc/skynet-staging/pr-1-backend" --context-entries "$(task_def "$td/skynet-staging-dbadmin:999")"
expect "$policy" allowed "delete a preview service" ecs:DeleteService "$svc/skynet-staging/pr-1-backend"
expect "$policy" denied "delete a preview-named service in production" ecs:DeleteService "$svc/skynet-production/pr-1-backend"
expect "$policy" allowed "run a preview migrate task" ecs:RunTask "$td/skynet-pr-1-migrate:999" --context-entries "$(cluster staging)"
expect "$policy" denied "run a preview migrate task in production" ecs:RunTask "$td/skynet-pr-1-migrate:999" --context-entries "$(cluster production)"
expect "$policy" allowed "modify a preview slot rule" elasticloadbalancing:ModifyRule "$rule" --context-entries "$(resource_tag preview)"
expect "$policy" denied "modify a staging rule" elasticloadbalancing:ModifyRule "$rule" --context-entries "$(resource_tag staging)"
expect "$policy" denied "create a rule tagged preview" elasticloadbalancing:CreateRule "$listener" --context-entries "$(tag preview)"
expect "$policy" denied "delete a preview slot rule" elasticloadbalancing:DeleteRule "$rule" --context-entries "$(resource_tag preview)"
expect "$policy" denied "change the priority of a preview slot rule" elasticloadbalancing:SetRulePriorities "$rule" --context-entries "$(resource_tag preview)"
expect "$policy" denied "tag a staging rule" elasticloadbalancing:AddTags "$rule" --context-entries "$(tag preview)"
expect "$policy" denied "create a target group" elasticloadbalancing:CreateTargetGroup "arn:aws:elasticloadbalancing:$region:$account:targetgroup/skynet-pr-1-backend/0" --context-entries "$(tag preview)"
expect "$policy" denied "delete the staging backend target group" elasticloadbalancing:DeleteTargetGroup "arn:aws:elasticloadbalancing:$region:$account:targetgroup/skynet-staging-backend/0"
for repository in buildcache-staging buildcache-production backend frontend litellm; do
  resource="arn:aws:ecr:$region:$account:repository/skynet/$repository"
  for action in ecr:PutImage ecr:InitiateLayerUpload ecr:UploadLayerPart ecr:CompleteLayerUpload \
    ecr:BatchDeleteImage ecr:PutLifecyclePolicy ecr:SetRepositoryPolicy \
    ecr:DeleteRepository ecr:PutImageTagMutability; do
    expect "$policy" denied "write $action to skynet/$repository" "$action" "$resource"
  done
done
for action in ecr:BatchCheckLayerAvailability ecr:BatchGetImage ecr:DescribeImages \
  ecr:GetDownloadUrlForLayer; do
  expect "$policy" allowed "read staging cache with $action" "$action" \
    "arn:aws:ecr:$region:$account:repository/skynet/buildcache-staging"
  expect "$policy" denied "read production cache with $action" "$action" \
    "arn:aws:ecr:$region:$account:repository/skynet/buildcache-production"
done
expect "$policy" allowed "push a preview cache image" ecr:PutImage \
  "arn:aws:ecr:$region:$account:repository/skynet/preview-buildcache"
expect "$policy" allowed "push a preview image" ecr:PutImage "arn:aws:ecr:$region:$account:repository/skynet/preview-backend"
expect "$policy" denied "push a staging image" ecr:PutImage "arn:aws:ecr:$region:$account:repository/skynet/backend"
expect "$policy" allowed "take a preview slot" ssm:PutParameter "arn:aws:ssm:$region:$account:parameter/skynet/preview/slots/1"
expect "$policy" denied "write a staging parameter" ssm:PutParameter "arn:aws:ssm:$region:$account:parameter/skynet/staging/deploy"

policy="$(policy_for data.aws_iam_policy_document.github_preview_cleanup)"
echo "-- preview cleanup role"
expect "$policy" allowed "delete a preview revision" ecs:DeleteTaskDefinitions "$td/skynet-pr-1-backend:999"
for env in staging production; do
  expect "$policy" denied "delete a $env backend revision" ecs:DeleteTaskDefinitions "$td/skynet-$env-backend:999"
  expect "$policy" denied "register a $env backend revision" ecs:RegisterTaskDefinition "$td/skynet-$env-backend:999" --context-entries "$(tag "$env")"
  expect "$policy" denied "update the $env backend service" ecs:UpdateService "$svc/skynet-$env/skynet-$env-backend"
done
expect "$policy" denied "take a preview slot" ssm:PutParameter "arn:aws:ssm:$region:$account:parameter/skynet/preview/slots/1"

# The simulator cannot evaluate a trust policy, so this reads it: every
# statement must allow only OIDC tokens whose subject is the main branch.
trust="$(policy_for 'data.aws_iam_policy_document.github_preview_cleanup_trust[0]')"
if jq -e '[.Statement[] | .Condition.StringEquals["token.actions.githubusercontent.com:sub"]
    | (if type == "array" then . else [.] end) | length == 1 and (.[0] | endswith(":ref:refs/heads/main"))]
    | length > 0 and all' <<<"$trust" >/dev/null &&
  jq -e '[.Statement[] | .Condition | (.StringLike // {}) | has("token.actions.githubusercontent.com:sub") | not] | all' <<<"$trust" >/dev/null; then
  echo "ok    only main can assume the preview cleanup role"
else
  echo "FAIL  the preview cleanup role trusts more than main: $(jq -c '[.Statement[].Condition]' <<<"$trust")"
  fails=$((fails + 1))
fi

if ((fails > 0)); then
  die "$fails isolation checks failed"
fi
echo "all isolation checks passed"
