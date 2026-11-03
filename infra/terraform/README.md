# Skynet on AWS (SKY-156)

Terraform for Skynet on AWS: ECS on Fargate (ARM64 / Graviton), RDS PostgreSQL 18,
ElastiCache Valkey, ALB with ACM, Secrets Manager, ECR and CloudWatch.
Region: `us-east-2`. AWS CLI profile: `skynet`.

`bootstrap` and `global` are applied in account 669242775630 (`us-east-2`).
The environment stacks follow "First apply (staging, then production)" below.

## Account prerequisites

- The `skynet` account is on the AWS **paid plan** (`aws freetier
  get-account-plan-state` shows `PAID` / `ACTIVE`, 2026-10-21). Set a spend
  limit in AWS Settings before the first apply.
- "Advanced AWS features" are on, so the managed SCP no longer denies
  `iam:*Provider*`. A read-only `aws iam list-open-id-connect-providers` works
  and returns an empty list. `global` creates the GitHub Actions OIDC provider
  and the GitHub roles (`enable_github_oidc` defaults to `true`).
- Spend limit: the plan is $460 per month for both environments. The lean
  variants (about $419) fit. The full variants (about $671) do not; raise the
  limit before you apply `full.tfvars` in both environments.

## Layout

| Path | Stack | Creates |
|------|-------|---------|
| `bootstrap/` | local state | The S3 state bucket (versioned, encrypted, TLS only). |
| `global/` | `global/terraform.tfstate` | ECR repos `skynet/backend`, `skynet/frontend`, `skynet/litellm`. GitHub OIDC provider and four GitHub roles (see "GitHub roles"; `enable_github_oidc`, default `true`). The `skynet/dev/*` secrets and the secret-change alarm (see "Secrets"). |
| `environments/staging/` | `staging/terraform.tfstate` | One environment from `modules/environment`. Sizing in `terraform.tfvars` (lean) and `full.tfvars`. |
| `environments/production/` | `production/terraform.tfstate` | The same module. Sizing in `terraform.tfvars` (lean) and `full.tfvars`. |
| `modules/environment/` | module | VPC, ALB, ACM, ECS cluster, services, task definitions, RDS, Valkey, secrets, logs, alarms. |
| `scripts/` | shell | Build and push, deploy (migrate first), secrets, database roles, DB tunnel. |
| `.checkov.yaml` | config | checkov skips, each with its reason. |

Remote state uses the S3 backend with native S3 locking (`use_lockfile = true`).
There is no DynamoDB table.

### One environment

- VPC with 2 AZs: public subnets (ALB, NAT), private subnets (tasks), isolated
  subnets (RDS, Valkey, no internet route). S3 gateway endpoint. VPC flow logs.
  With `egress_mode = "public_ip"` the tasks run in the public subnets instead
  and there is no NAT gateway.
- ALB: HTTP redirects to HTTPS. Idle timeout 120 s (SSE streams need >= 60 s).
  The API host goes to the backend. Every other host goes to the frontend.
- ECS services: `backend` (8080), `worker`, `frontend` (3001), `litellm` (4000).
  The backend and the worker reach litellm at `litellm.<env>.skynet.internal:4000`
  (Cloud Map). ECS Exec is on. Deployment circuit breaker with rollback is on.
- One-off task families: `migrate` (`python manage.py migrate`) and `dbadmin`
  (psql tools, for the database roles and the tunnel). The dbadmin
  image is `postgres:18-alpine` pinned by digest.
- RDS PostgreSQL 18.6 (`rds.force_ssl=1`, gp3, encrypted, Performance Insights).
  pgvector is an RDS extension; `create-db-roles.sh` enables it.
- Database users: backend, worker and migrate connect as `skynet_app`, the
  owner of the `skynet` database. litellm connects as `litellm`, the owner of
  the `litellm` database. Only the dbadmin task uses the RDS master user.
- Backups: 35 days in production (the RDS maximum), 3 days in staging.
- ElastiCache Valkey 8.1 (TLS, AUTH token). One shared cache for app and litellm,
  as on Railway.
- CloudWatch log group per family. Alarms go to one SNS topic: ALB 5xx, unhealthy
  targets, backend latency, ECS CPU and memory, a crashed task (from ECS stop
  events), no running tasks in 3 of 5 minutes, running tasks below desired for
  10 minutes, RDS CPU, memory, free storage and connections, Valkey CPU and
  memory, error lines in the logs, RDS failure events, and no finished
  automated RDS backup in 30 hours. The topic uses its own KMS key: CloudWatch
  and RDS cannot publish to a topic under the AWS managed SNS key.

### Choices

- **PostgreSQL 18.6, not 16.** The Railway source ran 18.6 (all four backup
  `info.json` files say `18.6 (Debian 18.6-1.pgdg13+2)`). `pg_restore` cannot
  load an 18 archive into an older server, and a downgrade adds a dump format
  risk for no gain. RDS supports 18.6, so the target matches the source.
- **One RDS instance holds two databases** (`skynet` and `litellm`), each with
  its own owner role. It saves the cost of a second instance
  (about $25 per month in staging, about $95 in production) and keeps backups and
  maintenance in one place. The databases and roles stay separate, so a split
  later is a `pg_dump` / `pg_restore`.
- **No RDS Proxy and no PgBouncer.** At 2 to 4 app tasks the SQLAlchemy pools stay
  well below `max_connections`. Direct connections keep advisory locks and session
  state unchanged. Add RDS Proxy if the task count grows.
- **Valkey, not Redis OSS.** It is protocol compatible and about 20% cheaper.
- **ARM64 tasks.** Images build natively on Apple silicon.
- **The frontend image is built per environment** (tag `<sha>-staging` or
  `<sha>-production`). Next.js puts `NEXT_PUBLIC_*` values into the bundle.
- Port numbers avoid 3000 and 8000.

## Lean and full variants

Every cost choice is a variable of the environment stack. `terraform.tfvars`
holds the **lean** variant and Terraform loads it automatically. `full.tfvars`
holds the **full** variant. To scale up, add `-var-file=full.tfvars` to every
`plan` and `apply`, or copy single values into `terraform.tfvars`. No `.tf`
file changes.

`terraform.tfvars` also holds the go-live switches `services_enabled` and
`enable_https`. They are `false` for pass 1 and stay `true` after step 8 of
"First apply". `full.tfvars` does not set them, so both variants keep them.

| Variable | Staging lean | Staging full | Production lean | Production full |
|----------|--------------|--------------|-----------------|-----------------|
| `egress_mode` | `public_ip` (no NAT) | `nat_single` | `nat_single` | `nat_per_az` |
| `db_instance_class` / `db_multi_az` | db.t4g.small / false | db.t4g.small / false | db.t4g.small / false | db.t4g.medium / true |
| `cache_node_count` | 1 | 1 | 1 | 2 (failover) |
| backend | 1 x 0.5 vCPU / 3 GB | 1 x 0.5 / 3 GB | 1 x 0.5 / 3 GB | 2 x 1 / 2 GB |
| worker | 1 x 0.5 / 3 GB | 1 x 0.5 / 3 GB | 1 x 0.5 / 3 GB | 2 x 1 / 2 GB |
| frontend | 1 x 0.25 / 0.5 GB | 1 x 0.25 / 0.5 GB | 1 x 0.25 / 1 GB | 2 x 0.5 / 1 GB |
| litellm | 1 x 0.5 / 3 GB | 1 x 0.5 / 3 GB | 1 x 0.5 / 3 GB | 2 x 0.5 / 2 GB |

Railway ran backend 3, worker 3, frontend 2 and litellm 2 replicas in
production. Their summed memory was backend 1.6 GB, worker 1.3 GB, litellm
1.3 GB and frontend 0.3 GB, and staging CPU stayed below 0.02 vCPU. One task
per service carries that load. A second task buys availability, not capacity.
`backend` and `frontend` still scale out to 3 tasks on CPU (target 60%).
The worker queue (`FOR UPDATE SKIP LOCKED`) lets more workers join at any time.

What each lean cut costs in availability:

| Cut | Saves per month | Availability trade-off |
|-----|-----------------|------------------------|
| 1 task per service, 0.5 vCPU tasks (production) | ~$111 | A task crash or an AZ outage stops that service for 1-3 minutes until ECS starts a replacement. Deploys stay zero-downtime. A single litellm task stops all LLM calls while it restarts. |
| Single-AZ RDS (production) | ~$73 with the class change | An instance failure or OS patch means minutes of database downtime. An AZ outage means a point-in-time restore into the other AZ (about 30-60 minutes, up to 5 minutes of writes lost). Deletion protection and 35-day backups stay. |
| db.t4g.small in place of medium (production) | included above | 2 GB RAM, about 200 connections. The burstable CPU bills extra credits under sustained load. |
| 1 Valkey node (production) | ~$9 | A node failure loses the cache and stops Valkey for minutes until ElastiCache replaces the node. App sessions and rate limits in Valkey reset. |
| 1 NAT gateway (production) | ~$37 | All egress goes through one AZ. If that AZ fails, tasks in the other AZ lose internet access (LLM providers, Stripe, OAuth). |
| No NAT, public task IPs (staging) | ~$22 net | No availability loss. Tasks have public IPv4 addresses; security groups still accept inbound traffic only from the ALB. Egress IPs change per task, so no third party can allowlist them. |

Options that the lean variant does not use:

- `spot = true` on staging saves about $46 per month. Staging used it until
  2026-10-29, when Spot had no capacity for hours and one-task services stayed
  down. AWS may also stop a Spot task with a 2-minute warning.
- `spot = true` on the production worker saves about $14 per month. A Spot stop
  interrupts running optimization jobs; orphan recovery then retries or fails
  them, and the user waits. Not worth $14 for paid customer jobs.
- `egress_mode = "public_ip"` in production saves about $22 per month, at the
  cost of public IPs on production tasks.
- db.t4g.micro (1 GB RAM, about 80 connections) saves about $12 per month. The
  pools allow 40 connections per task, so it is not sensible.
- Interface VPC endpoints do not replace NAT. The tasks call third-party APIs on
  the internet, and 7 endpoints in 2 AZs cost about $100 per month.

Scale-up order when traffic or risk grows: `desired_count = 2` for backend and
frontend (about $28), then `nat_per_az` (about $37), then `db_multi_az = true`
(about $26), then `cache_node_count = 2` (about $9).

Changes apply in place. The go-live switches in `terraform.tfvars` stay
`true`, so a sizing apply keeps the services and HTTPS. RDS class and Multi-AZ changes in production wait for
the Sunday maintenance window (`apply_immediately` is false there); add the
change in the console with "apply immediately" if it cannot wait. A
`spot` or `egress_mode` change starts a new deployment of the affected services.

## Secrets

Terraform creates these Secrets Manager secrets per environment. The task
definitions read single JSON keys by ARN (`<arn>:<KEY>::`).

| Secret | Values | Read by |
|--------|--------|---------|
| `skynet/<env>/app` | You fill them | backend, worker, migrate |
| `skynet/<env>/frontend` | You fill them | frontend |
| `skynet/<env>/litellm` | You fill them | litellm |
| `skynet/<env>/app-generated` | Terraform: `REMOTE_DB_URL` (as `skynet_app`), `REDIS_URL`, `APP_DB_PASSWORD` | backend, worker, migrate (the URLs); dbadmin (`APP_DB_PASSWORD`) |
| `skynet/<env>/litellm-generated` | Terraform: `LITELLM_DATABASE_URL` (as `litellm`), `REDIS_PASSWORD`, `LITELLM_DB_PASSWORD` | litellm (the URL and Redis password); dbadmin (`LITELLM_DB_PASSWORD`) |
| `skynet/<env>/db-admin` | Terraform: the RDS master `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` | dbadmin |

The key names come from the Railway export (SKY-155). ECS does not start a task
when a referenced key is missing. An empty string is allowed.

Load the values (key names only pass through the script, never values):

```bash
cd infra/terraform
scripts/secrets.sh template staging app      > ~/skynet-secrets/staging-app.json
scripts/secrets.sh template staging frontend > ~/skynet-secrets/staging-frontend.json
scripts/secrets.sh template staging litellm  > ~/skynet-secrets/staging-litellm.json
# Fill the values in those files. Keep them outside the repo.
scripts/secrets.sh put staging app      ~/skynet-secrets/staging-app.json
scripts/secrets.sh put staging frontend ~/skynet-secrets/staging-frontend.json
scripts/secrets.sh put staging litellm  ~/skynet-secrets/staging-litellm.json
scripts/secrets.sh check staging
```

Delete the local JSON files after `put`. The generated values are also in the
Terraform state; the state bucket is private and encrypted.

### Change one key

`set` changes one key and keeps every other key. It reads the value from a
hidden prompt, so the value is never on a command line or on the screen. It
prints the key name only. The new JSON goes through a private temporary file
(mode 600) that the script deletes on exit.

```bash
scripts/secrets.sh set staging app OPENROUTER_API_KEY   # prompts for the value
scripts/secrets.sh check-empty staging                  # keys with an empty value
```

Secrets Manager keeps the previous value as `AWSPREVIOUS`. Two `set` runs at
the same time on one secret can lose one change, so run them one at a time.

### Local development keys (`skynet/dev/*`)

The `global` root creates `skynet/dev/app`, `skynet/dev/frontend` and
`skynet/dev/litellm` with no value. Put dev-only keys there (for example a
separate OpenRouter key with a low limit), never staging or production
values:

```bash
scripts/secrets.sh set dev app OPENROUTER_API_KEY
```

`scripts/dev-env.sh` (from the repository root, after `just setup`) copies
the non-empty keys into the worktree's git-ignored `backend/.env`,
`frontend/.env.local` and `deploy/litellm/.env`. It changes only those keys,
skips a missing file, and prints key names only. It skips a value that
contains a newline, `'`, `$` or `\` and names the key; add that key by hand. Agents run it instead of
reading staging or production secrets.

### Secret-change alarm

The `global` root has a CloudTrail trail (write events, us-east-2) and an
EventBridge rule. Every `PutSecretValue`, `UpdateSecret`, `DeleteSecret`,
`RestoreSecret`, `UpdateSecretVersionStage`, `RotateSecret` or resource policy
change on a `skynet/*` secret sends an email through the SNS topic
`skynet-secret-changes`. The message names the call, the secret, the caller
and the time, never a value. Set the receiver with
`export TF_VAR_alert_email=<address>` before every global plan or apply, then
confirm the subscription email once. A global apply without it deletes the
subscription (the plan warns: `alert_email is empty`). The CI plan runs
without it, so after the first apply each PR plan shows the email
subscription as `1 to destroy`. That is expected; CI never applies.

### Secret scan

`.githooks/pre-commit` runs gitleaks on staged changes (`just setup` turns
the hooks on), and `.github/workflows/gitleaks.yml` scans the full history on
every PR.

## Build and push images

The scripts require Bash 4 or newer. macOS's bundled Bash 3.2 is not supported.

```bash
aws login --profile skynet
infra/terraform/scripts/build-push.sh staging <git-sha>
```

The script runs `docker buildx build --platform linux/arm64 --push` for
`backend` (`backend/`), `frontend` (`frontend/`) and `litellm` (`deploy/litellm/`). ECR tags are
immutable; the script skips a tag that exists. Frontend build values
(`NEXT_PUBLIC_SENTRY_DSN` and others) come from the shell. The frontend build
does not generate or upload Sentry source maps. ECR's 100-image count uses the
original push time; adding a tag does not refresh an image's age.

Manual equivalent for one image:

```bash
REG=<account>.dkr.ecr.us-east-2.amazonaws.com
aws ecr get-login-password --profile skynet --region us-east-2 | docker login --username AWS --password-stdin "$REG"
docker buildx build --platform linux/arm64 -t "$REG/skynet/backend:<sha>" --push backend
```

## First apply (staging, then production)

Prerequisites: Terraform >= 1.11, AWS CLI v2, Docker with buildx, `jq`,
the Session Manager plugin (`brew install --cask session-manager-plugin`) and
`postgresql@18` for `psql` through the DB tunnel.

```bash
aws login --profile skynet
export AWS_PROFILE=skynet AWS_REGION=us-east-2
cd infra/terraform

# 1. State bucket (local state, once per account).
terraform -chdir=bootstrap init
terraform -chdir=bootstrap apply
BUCKET=$(terraform -chdir=bootstrap output -raw state_bucket)

# 2. Global: ECR repos, the GitHub OIDC provider and the GitHub roles.
terraform -chdir=global init -backend-config="bucket=$BUCKET"
terraform -chdir=global apply

# 3. Images.
SHA=$(git rev-parse HEAD)
scripts/build-push.sh staging "$SHA"

# 4. Environment, pass 1: network, ALB (HTTP only), RDS, Valkey, secrets,
#    task definitions. No ECS services yet: terraform.tfvars has
#    services_enabled = false and enable_https = false.
terraform -chdir=environments/staging init -backend-config="bucket=$BUCKET"
#    terraform.tfvars (lean) loads automatically; add -var-file=full.tfvars
#    to every apply for the full variant.
terraform -chdir=environments/staging apply -var image_tag="$SHA"
terraform -chdir=environments/staging output acm_validation_records

# 5. Give the ACM validation records to Gilad. He creates them by hand.

# 6. Secrets (see "Secrets"), then the database roles (skynet_app, litellm),
#    the litellm database and pgvector.
scripts/secrets.sh put staging app      ~/skynet-secrets/staging-app.json
scripts/secrets.sh put staging frontend ~/skynet-secrets/staging-frontend.json
scripts/secrets.sh put staging litellm  ~/skynet-secrets/staging-litellm.json
scripts/secrets.sh check staging
scripts/create-db-roles.sh staging

# 7. Migrate (creates the schema in the empty databases).
scripts/deploy.sh staging "$SHA" --migrate-only

# 8. Environment, pass 2: services on, HTTPS on (after ACM shows ISSUED).
#    In environments/staging/terraform.tfvars, set services_enabled = true
#    and enable_https = true. Apply, then merge that change through a PR.
#    The switches must stay true: a later plan or apply that reads false
#    destroys the 4 services, their scaling and alarms, and the HTTPS
#    listener. The CI plan fails when a plan would destroy them.
aws acm list-certificates --query 'CertificateSummaryList[].[DomainName,Status]' --output table
terraform -chdir=environments/staging apply -var image_tag="$SHA"

# 9. Check: ECS services stable, targets healthy, then the DNS cutover.
```

For production, repeat steps 3 to 9 with `production` (step 8 edits
`environments/production/terraform.tfvars`). Build the production
frontend image separately: `scripts/build-push.sh production "$SHA"`.
Optional: `alert_email = "<address>"` in `terraform.tfvars` subscribes an email
to the alarms. The repository is public, so you can instead subscribe once
outside Terraform, which later applies leave alone:
`aws sns subscribe --topic-arn <alarm topic ARN> --protocol email --notification-endpoint <address>`.
Click the confirmation link in the email from AWS; until then nothing is sent.

## Deploys after the first apply

```bash
infra/terraform/scripts/build-push.sh staging <sha>
infra/terraform/scripts/deploy.sh staging <sha>
```

`deploy.sh` registers new task definition revisions with the new image,
runs the `migrate` task, stops if it fails, then updates the services and waits.
It starts each new revision from the newest revision of the family whose
execution and task roles are `skynet-<env>-<family>-exec` and `-task`, tags
it `Environment=<env>`, and runs the exact migrate revision that it
registered.
Rollback: run `deploy.sh` with the previous SHA. Migrations do not roll back.

Terraform ignores the service `task_definition` and `desired_count`. A Terraform
change to a task definition (for example a new secret key) takes effect at the
next `deploy.sh` run.

`.github/workflows/aws-deploy.yml` does the same with the OIDC role of the
chosen environment. A push to `staging` deploys staging. Production runs only
from `workflow_dispatch`. The GitHub environment `aws-production` is protected
(`main` branch only, Gilad's approval required) and has no variables yet. See
"GitHub roles" for the setup.

## GitHub roles and CI

`global` creates one GitHub OIDC provider and four roles. None has static keys.

| Role | Trusted OIDC subject | Reaches |
|------|----------------------|---------|
| `skynet-github-deploy-staging` | `repo:<repo>:environment:aws-staging` | ECR push (images are shared), task definitions tagged `Environment=staging` in the `skynet-staging-*` families, the `skynet-staging` services, RunTask of `skynet-staging-migrate` only, PassRole on the exact staging task and execution roles, `/ecs/skynet-staging/*` logs, `/skynet/staging/*` SSM. |
| `skynet-github-deploy-production` | `repo:<repo>:environment:aws-production` | The same, for production resources only. |
| `skynet-github-terraform-plan` | `repo:<repo>:pull_request` | `ReadOnlyAccess`, the state objects, and `GetSecretValue` on the generated secrets (Terraform refresh reads their versions). |
| `skynet-github-terraform-apply` | `repo:<repo>:ref:refs/heads/staging` and `.../main` | **Admin-equivalent.** `PowerUserAccess` plus all IAM actions on `role/skynet-staging-*` and `role/skynet-production-*`. It can create a role there with any policy and pass it to a task, so it can reach any AWS permission. It cannot change the `skynet-github-*` roles or the OIDC provider directly, but it can through a `skynet-<env>-*` role that it creates (for example with `AdministratorAccess`) and assumes. No permissions boundary stops this. No workflow uses it yet. |

No deploy role can run or pass the roles of the `dbadmin` task family; only a
person with their own AWS login runs dbadmin.

Environment isolation: `RegisterTaskDefinition` has no resource-level scope in
IAM. Each deploy role may register a revision only with the request tag
`Environment=<its env>`, and ECS checks `ecs:TagResource` on the new revision,
which each role has only for its own families. A role also cannot pass the
other environment's task roles, and `deploy.sh` skips any revision that does
not carry its own environment's roles. `scripts/check-deploy-isolation.sh
<global-plan.json>` proves this with the IAM policy simulator (read-only); the
`terraform` workflow runs it after the global plan.

The apply role is the strongest role. Its trust (staging and main branches
only) and branch protection are the real control. The `global` stack stays a
manual apply by a person. That is a process rule, not an IAM control: the
apply role can change the global resources through a role that it creates and
assumes.

Set up GitHub (Settings > Environments) after `global` is applied:

1. `aws-staging`: deployment branches = `staging` only. Variable
   `AWS_DEPLOY_ROLE_ARN` = `github_deploy_role_arns["staging"]`.
2. `aws-production`: deployment branches = `main` only. Required reviewers =
   Gilad. Variable `AWS_DEPLOY_ROLE_ARN` = `github_deploy_role_arns["production"]`.
3. Repository variables for the PR plan: `AWS_PLAN_ROLE_ARN` =
   `github_plan_role_arn` and `TF_STATE_BUCKET` = `state_bucket_name`.

The branch rules matter: the OIDC trust checks only the environment name, and
GitHub enforces which branch may use that environment. The staging pipeline
builds the images that production also runs (ECR is shared), so keep the
`aws-staging` branch rule on.

`.github/workflows/terraform.yml` runs on every PR that changes
`infra/terraform`: `fmt -check`, `validate` for every root, tflint, checkov,
then a read-only `plan -lock=false` of `global`, `staging` and `production`. It
posts a PR comment with the `Plan:` lines and, when present, the addresses of
the ECS services or HTTPS listener that the plan would delete. It never
applies. The plan job fails when an environment plan would delete an ECS
service or the HTTPS listener (for example a go-live switch set back to
`false`). The plan job
skips with a notice when the PR comes from a fork (no OIDC token) or when the
two variables are not set.

Risk to know: the plan role can read the state, and the state holds the
generated database and cache passwords. Any same-repository PR branch can use
the plan role. Only people with write access can push such a branch.

## Restore

The October 2026 cutover restored the Railway pg_dump files into RDS once.
Railway is deleted (SKY-185), so its restore script is gone (SKY-194). A new environment
starts with empty databases, and `deploy.sh <env> <sha> --migrate-only` creates
the schema. To recover data, restore an RDS automated backup (point-in-time
restore) into a new instance.

## DNS cutover

DNS changes are manual and need Gilad's approval. Terraform does not touch DNS.

1. Lower the TTL of the current records to 60 s one day before.
2. Create the ACM validation CNAMEs (output `acm_validation_records`). Keep them;
   ACM uses them for renewal.
3. Point the hosts at the ALB (`terraform output alb_dns_name`):
   - staging: `staging.skynetml.com` and `api-staging.skynetml.com` -> CNAME.
   - production: `www.skynetml.com` and `api.skynetml.com` -> CNAME.
     `skynetml.com` (apex) needs an ALIAS / ANAME / CNAME-flattening record, or
     Route 53 with an alias to `alb_dns_name` / `alb_zone_id`.
4. Check `https://<host>/` and the backend health check through the new names.
5. Update the OAuth redirect URIs and the Stripe webhook endpoint only if the
   host names change. They do not change in this plan.

### Remote MCP host (SKY-229)

`mcp_domain` adds the remote MCP host to the API listener rule. The backend
serves the MCP endpoint at `https://<mcp_domain>/mcp` (`MCP_RESOURCE_URL`).

- staging: `mcp.staging.skynetml.com` uses the `*.staging.skynetml.com`
  wildcard certificate and CNAME. No new DNS record is needed.
- production: `mcp.skynetml.com` gets its own certificate. Run
  `terraform apply -target=module.env.aws_acm_certificate.mcp` first. Create
  its record from `acm_validation_records`, then run the full apply. Point
  `mcp.skynetml.com` at the ALB with a CNAME.
- PR previews: `scripts/preview.sh` sets the URL to
  `https://api-pr-<n>.staging.skynetml.com/remote-mcp`, under the preview API
  host. The wildcard certificate has no second preview host.

## Monthly cost estimate (us-east-2, October 2026 list prices)

Prices from the AWS Price List API: Fargate ARM $0.03238 per vCPU-hour and
$0.00356 per GB-hour, NAT gateway $0.045 per hour, public IPv4 $0.005 per hour,
db.t4g.small $0.032 per hour (Multi-AZ medium $0.129), cache.t4g.micro Valkey
$0.0128 per hour, gp3 $0.115 per GB-month, 730 hours per month. Fargate Spot is
not in the Price List API; the estimate assumes 70% off (AWS: "up to 70%").

| Item | Staging lean | Staging full | Production lean | Production full |
|------|--------------|--------------|-----------------|-----------------|
| Fargate ARM64 tasks | ~$66 (4 tasks) | ~$66 (4 tasks) | ~$67 (4 tasks, 1.75 vCPU) | ~$178 (8 tasks, 6 vCPU) |
| RDS PostgreSQL, 20 GB gp3 | ~$26 (small, single-AZ) | ~$26 | ~$26 (small, single-AZ) | ~$99 (medium, Multi-AZ) |
| ElastiCache Valkey | ~$9 (1 node) | ~$9 | ~$9 (1 node) | ~$19 (2 nodes) |
| ALB (hours + LCU) | ~$20 | ~$20 | ~$25 | ~$25 |
| NAT gateway hours | $0 | ~$33 (1) | ~$33 (1) | ~$66 (2) |
| Public IPv4 | ~$22 (2 ALB + 4 tasks) | ~$11 (3) | ~$11 (3) | ~$15 (4) |
| CloudWatch (Container Insights about $40, logs, alarms, monitoring) | ~$45 | ~$45 | ~$50 | ~$50 |
| Secrets Manager, S3, ECR storage | ~$5 | ~$5 | ~$5 | ~$5 |
| **Total** | **~$193** | **~$215** | **~$226** | **~$456** |

Lean total: about $419 per month, below the $460 spend limit. Full total:
about $671 per month, above the $460 spend limit. Production full alone
(about $456) uses almost all of it.

Container Insights is the largest CloudWatch item: about $40-50 per month per
environment (custom metrics per task and service, plus performance log
ingestion). Turning it off saves that, at the cost of the per-task CPU and
memory views and the "tasks-missing" and "tasks-below-desired" alarms,
which read Container Insights metrics.

Not included: data transfer out, NAT data processing ($0.045 per GB), LLM provider
costs, RDS CPU credits beyond the baseline, WAF (off by default; about $10 per
month plus requests when on). Autoscaled tasks add about $20 (backend) or $9
(frontend) per task-month, plus $3.65 per public IP in `public_ip` mode.
The 35-day production backups add a few dollars of backup storage beyond the
free allowance (the database size). The ECR repos and the
state bucket are shared and cost a few dollars per month. A Compute Savings
Plan cuts on-demand Fargate by about 20% to 50%.

## Not in this stack

- PR previews (SKY-164): out of scope. The module can serve a third
  environment later.
- The app code changes for `APP_GIT_SHA`, `APP_ENVIRONMENT`, `APP_SERVICE_NAME`
  and `FORWARDED_ALLOW_IPS` are in PR #788 (SKY-161). The task definitions set
  these variables already.
- DNS, OAuth and Stripe configuration.

## Plan

The environment stacks build the ECR URLs from the account ID, so they plan
before `global` exists. To plan without the state bucket, copy `infra/terraform` to a
temporary folder, add `backend_override.tf` with `terraform { backend "local" {} }`
to each stack, then run `terraform init` and `terraform plan -var image_tag=<sha>`.

Results on 2026-10-21 (account 669242775630, us-east-2, empty account):

| Stack | Pass 1 | Pass 2 (`services_enabled`, `enable_https`) |
|-------|--------|------|
| bootstrap | 7 to add | - |
| global | 17 to add (3 ECR repos, 3 lifecycle policies, OIDC provider, 2 deploy roles and 2 policies, plan role with policy and attachment, apply role with policy and attachment) | - |
| staging lean | 152 to add | 173 to add |
| staging full (`-var-file=full.tfvars`) | 156 to add | 177 to add |
| production lean | 156 to add | 177 to add |
| production full (`-var-file=full.tfvars`) | 158 to add | 179 to add |

Staging lean has no NAT gateway, Elastic IP or NAT routes (4 fewer). Production
lean has one NAT gateway and one Elastic IP (2 fewer than full). Pass 2 adds 21
resources in every variant: 4 services, 2 scaling targets, 2 scaling policies,
12 service alarms and the HTTPS listener.

## Checks

```bash
cd infra/terraform
terraform fmt -recursive -check
for d in bootstrap global environments/staging environments/production; do
  terraform -chdir=$d init -backend=false && terraform -chdir=$d validate
done
tflint --init && tflint --recursive --config "$PWD/.tflint.hcl"
trivy config --severity HIGH,CRITICAL .
checkov --directory . --config-file .checkov.yaml
```

Trivy findings that the design accepts carry a `#trivy:ignore` comment with the
reason: open task egress (LLM providers, OAuth, Stripe, webhooks), the public ALB,
the HTTP listener (redirect only), and AWS-managed KMS keys in place of CMKs.
