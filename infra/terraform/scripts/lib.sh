#!/usr/bin/env bash
# Shared helpers for the deploy scripts. Source this file; do not run it.
# Never print secret values here: these scripts handle secret names only.

set -euo pipefail

# Local runs use the `skynet` CLI profile; GitHub Actions uses OIDC credentials.
if [[ -z "${GITHUB_ACTIONS:-}" ]]; then
  export AWS_PROFILE="${AWS_PROFILE:-skynet}"
fi
export AWS_REGION="${AWS_REGION:-us-east-2}"
export AWS_PAGER=""

die() {
  echo "error: $*" >&2
  exit 1
}

need() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "missing command: $c"
  done
}

refresh_aws_credentials() {
  local token_response token credentials access_key secret_key session_token
  if [[ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" &&
    -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" &&
    -z "${AWS_DEPLOY_ROLE_ARN:-}" ]]; then
    return 0
  fi
  [[ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" && -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" &&
    -n "${AWS_DEPLOY_ROLE_ARN:-}" ]] || die "incomplete GitHub OIDC credential configuration"
  command -v curl >/dev/null 2>&1 || die "missing command: curl"
  token_response="$(curl --fail --silent --show-error \
    -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=sts.amazonaws.com")" ||
    die "cannot request GitHub OIDC token"
  token="$(jq -er '.value' <<<"$token_response")" || die "GitHub OIDC response has no token"
  credentials="$(aws sts assume-role-with-web-identity \
    --role-arn "$AWS_DEPLOY_ROLE_ARN" --role-session-name skynet-deploy \
    --web-identity-token "$token" --duration-seconds 900 --output json)" ||
    die "cannot assume AWS deploy role with GitHub OIDC"
  access_key="$(jq -er '.Credentials.AccessKeyId' <<<"$credentials")" ||
    die "OIDC credentials have no access key"
  secret_key="$(jq -er '.Credentials.SecretAccessKey' <<<"$credentials")" ||
    die "OIDC credentials have no secret key"
  session_token="$(jq -er '.Credentials.SessionToken' <<<"$credentials")" ||
    die "OIDC credentials have no session token"
  export AWS_ACCESS_KEY_ID="$access_key"
  export AWS_SECRET_ACCESS_KEY="$secret_key"
  export AWS_SESSION_TOKEN="$session_token"
}

check_env() {
  case "${1:-}" in
    staging | production) ;;
    *) die "environment must be staging or production" ;;
  esac
}

# Prints the JSON that Terraform writes to SSM /skynet/<env>/deploy.
deploy_config() {
  aws ssm get-parameter --name "/skynet/$1/deploy" --query Parameter.Value --output text
}

# Runs a one-off task and waits for it to stop. Prints the task ARN.
# Args: env family [overrides-json] [task-definition-arn]
# Without a task definition ARN it runs the latest revision of the family.
run_task() {
  local env="$1" family="$2" overrides="${3:-}" td="${4:-}" cfg subnets sg cluster arn public_ip
  cfg="$(deploy_config "$env")"
  cluster="$(jq -r .cluster <<<"$cfg")"
  subnets="$(jq -r '.subnets | join(",")' <<<"$cfg")"
  # Without NAT (egress_mode = public_ip) a task needs a public IP to pull
  # images and read secrets.
  public_ip="$(jq -r 'if .assign_public_ip then "ENABLED" else "DISABLED" end' <<<"$cfg")"
  sg="$(jq -r --arg f "$family" '.security_groups[$f]' <<<"$cfg")"
  local args=(
    --cluster "$cluster"
    --task-definition "${td:-$(jq -r --arg f "$family" '.families[$f]' <<<"$cfg")}"
    --launch-type FARGATE
    --enable-execute-command
    --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=$public_ip}"
    --query 'tasks[0].taskArn' --output text
  )
  if [[ -n "$overrides" ]]; then
    args+=(--overrides "$overrides")
  fi
  arn="$(aws ecs run-task "${args[@]}")"
  [[ "$arn" == arn:* ]] || die "run-task did not start a task"
  echo "$arn"
}

# Waits for a task to stop and fails unless the container exit code is 0.
wait_task_ok() {
  local env="$1" arn="$2" cluster code reason
  cluster="$(deploy_config "$env" | jq -r .cluster)"
  # `aws ecs wait tasks-stopped` gives up after 10 minutes; migrations may run longer.
  local i status
  for ((i = 0; i < 360; i++)); do
    status="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$arn" --query 'tasks[0].lastStatus' --output text)"
    [[ "$status" == "STOPPED" ]] && break
    sleep 10
  done
  [[ "$status" == "STOPPED" ]] || die "task $arn still $status after 60 minutes"
  code="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$arn" --query 'tasks[0].containers[0].exitCode' --output text)"
  reason="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$arn" --query 'tasks[0].stoppedReason' --output text)"
  [[ "$code" == "0" ]] || die "task $arn failed (exit=$code, reason=$reason). Logs: CloudWatch /ecs/skynet-$env/*"
}
