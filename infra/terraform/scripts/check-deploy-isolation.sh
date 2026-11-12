#!/usr/bin/env bash
# Proves that each GitHub deploy role cannot change the other environment.
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
  for action in ecr:BatchDeleteImage ecr:CompleteLayerUpload ecr:DeleteRepository \
    ecr:InitiateLayerUpload ecr:PutImage ecr:PutImageTagMutability \
    ecr:PutLifecyclePolicy ecr:SetRepositoryPolicy ecr:UploadLayerPart; do
    expect "$policy" denied "write $action to the $other build cache" "$action" \
      "arn:aws:ecr:$region:$account:repository/skynet/buildcache-$other"
  done
  for action in ecr:BatchCheckLayerAvailability ecr:BatchGetImage ecr:DescribeImages \
    ecr:DescribeRepositories ecr:GetDownloadUrlForLayer; do
    expect "$policy" allowed "read the $env build cache with $action" "$action" \
      "arn:aws:ecr:$region:$account:repository/skynet/buildcache-$env"
    expect "$policy" denied "read the $other build cache with $action" "$action" \
      "arn:aws:ecr:$region:$account:repository/skynet/buildcache-$other"
  done
done

if ((fails > 0)); then
  die "$fails isolation checks failed"
fi
echo "all isolation checks passed"
