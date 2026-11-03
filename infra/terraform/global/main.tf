# Account-wide resources shared by staging and production:
# - ECR repositories (one image is built once and promoted by tag), and
#   separate repositories for PR preview images.
# - The GitHub Actions OIDC provider and its roles (no static AWS keys): one
#   deploy role per environment, a PR preview role, a read-only plan role for
#   pull requests and an apply role limited to the staging and main branches.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
  }

  # Partial configuration: pass the bucket at init time.
  #   terraform init -backend-config="bucket=<bootstrap output state_bucket>"
  backend "s3" {
    key          = "global/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "skynet"
      ManagedBy = "terraform"
      Stack     = "global"
    }
  }
}

variable "region" {
  description = "AWS Region."
  type        = string
  default     = "us-east-2"

  # The AWS account (new "project" experience) may create Regional resources
  # only in us-east-2.
  validation {
    condition     = var.region == "us-east-2"
    error_message = "This AWS account allows Regional resources only in us-east-2."
  }
}

variable "github_oidc_subject_prefix" {
  # The repository uses GitHub's immutable OIDC subject (owner@id/name@id), so
  # tokens carry the IDs, not the plain owner/name. Read the current value with
  # gh api repos/<owner>/<name>/actions/oidc/customization/sub.
  description = "OIDC sub claim prefix of the GitHub repository that may assume the GitHub roles."
  type        = string
  default     = "repo:Skynet-ml@350892321/skynet@1279192708"
}

variable "enable_github_oidc" {
  description = <<-EOT
    Create the GitHub Actions OIDC provider and its roles. The account
    has "advanced AWS features" on, so the managed SCP no longer denies
    iam:*Provider*. Set false only if that deny comes back; deploys then run
    from a laptop with scripts/deploy.sh.
  EOT
  type        = bool
  default     = true
}

variable "image_repositories" {
  description = "ECR repositories to create."
  type        = set(string)
  default     = ["backend", "frontend", "litellm"]
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

resource "aws_ecr_repository" "this" {
  for_each = var.image_repositories

  name = "skynet/${each.key}"
  # Tags are commit SHAs (plus an environment suffix for the frontend), so a
  # tag never moves. Immutable tags make a rollback point at exactly the image
  # that ran before.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Delete untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the newest 100 tagged images"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = 100
        }
        action = { type = "expire" }
      },
    ]
  })
}

resource "aws_ecr_repository" "buildcache_staging" {
  name = "skynet/buildcache-staging"
  # Checkov CKV_AWS_51 requires immutable cache tags.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "buildcache_staging" {
  repository = aws_ecr_repository.buildcache_staging.name

  policy = jsonencode({
    rules = concat([
      {
        rulePriority = 1
        description  = "Delete untagged cache images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      ], [
      for index, prefix in ["backend-", "litellm-", "frontend-staging-"] : {
        rulePriority = index + 2
        description  = "Keep newest five ${prefix} cache images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = [prefix]
          countType     = "imageCountMoreThan"
          countNumber   = 5
        }
        action = { type = "expire" }
      }
    ])
  })
}

resource "aws_ecr_repository" "buildcache_production" {
  name = "skynet/buildcache-production"
  # Checkov CKV_AWS_51 requires immutable cache tags.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "buildcache_production" {
  repository = aws_ecr_repository.buildcache_production.name

  policy = jsonencode({
    rules = concat([
      {
        rulePriority = 1
        description  = "Delete untagged cache images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      ], [
      for index, prefix in ["backend-", "litellm-", "frontend-production-"] : {
        rulePriority = index + 2
        description  = "Keep newest five ${prefix} cache images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = [prefix]
          countType     = "imageCountMoreThan"
          countNumber   = 5
        }
        action = { type = "expire" }
      }
    ])
  })
}

# Preview-to-preview cache poisoning cannot affect staging or production;
# each PR reads only its own prefix.
resource "aws_ecr_repository" "preview_buildcache" {
  name = "skynet/preview-buildcache"
  # Checkov CKV_AWS_51 requires immutable cache tags.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "preview_buildcache" {
  repository = aws_ecr_repository.preview_buildcache.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Delete untagged preview cache images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep newest 40 preview cache images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["preview-"]
          countType     = "imageCountMoreThan"
          countNumber   = 40
        }
        action = { type = "expire" }
      },
    ]
  })
}

# PR preview images (SKY-164) live in their own repositories. The preview
# role builds from any branch, so it must not push a tag that staging or
# production could later deploy from skynet/backend or skynet/frontend.
resource "aws_ecr_repository" "preview" {
  for_each = toset(["backend", "frontend"])

  name                 = "skynet/preview-${each.key}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

# scripts/preview.sh deletes a PR's images when it deletes the preview. These
# rules remove what a failed delete leaves. A live preview is redeployed at
# least every 48 hours or deleted, and a redeploy pushes an image again when
# it is older than 11 days, so 14 days never removes a running image.
resource "aws_ecr_lifecycle_policy" "preview" {
  for_each   = aws_ecr_repository.preview
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Delete untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Delete PR images after 14 days"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*-pr-*"]
          countType      = "sinceImagePushed"
          countUnit      = "days"
          countNumber    = 14
        }
        action = { type = "expire" }
      },
    ]
  })
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.enable_github_oidc ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_oidc_provider_arn = one(aws_iam_openid_connect_provider.github[*].arn)

  # Read by the plan role (PR plans) and written by the apply role.
  state_bucket_arn = "arn:aws:s3:::skynet-tfstate-${local.account_id}-${var.region}"

  # Families whose task definitions the deploy pipeline registers and whose
  # roles it may pass. dbadmin is left out on purpose: it reads the RDS master
  # secret, so only an operator with the skynet profile starts it.
  deploy_families = ["backend", "worker", "frontend", "litellm", "migrate"]

  # Built from the names, not from the resources, so the deploy policies are
  # known at plan time and scripts/check-deploy-isolation.sh can test them
  # before the first apply.
  # Preview builds use a separate cache so they cannot poison deploy layers.
  environments = toset(["staging", "production"])
  ecr_repository_arns = {
    staging = concat(
      [for r in var.image_repositories : "arn:aws:ecr:${var.region}:${local.account_id}:repository/skynet/${r}"],
      ["arn:aws:ecr:${var.region}:${local.account_id}:repository/skynet/buildcache-staging"],
    )
    production = concat(
      [for r in var.image_repositories : "arn:aws:ecr:${var.region}:${local.account_id}:repository/skynet/${r}"],
      ["arn:aws:ecr:${var.region}:${local.account_id}:repository/skynet/buildcache-production"],
    )
  }
}

# ---------------------------------------------------------------- deploy roles
# One role per environment. Each trusts only its own GitHub environment and
# reaches only its own cluster, services, task roles, logs and parameters.
# Task definitions: the deploy roles allow RegisterTaskDefinition on every
# family (resource "*") and use a tag condition instead: each role must tag a
# new revision Environment=<env>. ECS authorizes that tag
# as ecs:TagResource on the new revision, which EcsTags allows only for the
# role's own families. scripts/deploy.sh also uses only revisions whose
# execution and task roles are its own environment's roles; PassTaskRoles
# keeps the staging role from putting production roles on any revision.
# scripts/check-deploy-isolation.sh tests these rules with the IAM simulator.
# Configure in GitHub (README): the aws-staging environment deploys from the
# staging branch only; aws-production deploys from main only and needs a
# required reviewer.

data "aws_iam_policy_document" "github_deploy_trust" {
  for_each = var.enable_github_oidc ? local.environments : toset([])

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_oidc_subject_prefix}:environment:aws-${each.key}"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  for_each = var.enable_github_oidc ? local.environments : toset([])

  name                 = "skynet-github-deploy-${each.key}"
  description          = "Assumed by GitHub Actions (OIDC, environment aws-${each.key}) to push images and deploy ${each.key} ECS services."
  assume_role_policy   = data.aws_iam_policy_document.github_deploy_trust[each.key].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_deploy" {
  for_each = local.environments

  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Images are shared: production runs the backend and litellm images that
  # the staging pipeline built. Branch rules on aws-staging protect them.
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = local.ecr_repository_arns[each.key]
  }

  statement {
    sid = "EcsTaskDefinitionsUnscoped"
    # No resource-level permissions for these read actions.
    actions = [
      "ecs:DescribeTaskDefinition",
      "ecs:ListTaskDefinitions",
    ]
    resources = ["*"]
  }

  # deploy.sh reads the Environment tag with describe-task-definition
  # --include TAGS, which may need this action.
  statement {
    sid       = "EcsReadTaskDefinitionTags"
    actions   = ["ecs:ListTagsForResource"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-${each.key}-*:*"]
  }

  statement {
    sid       = "EcsRegisterTaskDefinitions"
    actions   = ["ecs:RegisterTaskDefinition"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Environment"
      values   = [each.key]
    }
  }

  statement {
    sid = "EcsServices"
    actions = [
      "ecs:DescribeServices",
      "ecs:UpdateService",
    ]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:service/skynet-${each.key}/*"]
  }

  # Only the migrate family runs as a one-off task from the pipeline.
  statement {
    sid       = "EcsRunMigrate"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-${each.key}-migrate:*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["arn:aws:ecs:${var.region}:${local.account_id}:cluster/skynet-${each.key}"]
    }
  }

  statement {
    sid       = "EcsTasks"
    actions   = ["ecs:DescribeTasks"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task/skynet-${each.key}/*"]
  }

  statement {
    sid     = "EcsTags"
    actions = ["ecs:TagResource"]
    resources = concat(
      [for f in local.deploy_families : "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-${each.key}-${f}:*"],
      ["arn:aws:ecs:${var.region}:${local.account_id}:task/skynet-${each.key}/*"],
    )
  }

  statement {
    sid     = "PassTaskRoles"
    actions = ["iam:PassRole"]
    resources = flatten([
      for f in local.deploy_families : [
        "arn:aws:iam::${local.account_id}:role/skynet-${each.key}-${f}-exec",
        "arn:aws:iam::${local.account_id}:role/skynet-${each.key}-${f}-task",
      ]
    ])

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid       = "ReadMigrationLogs"
    actions   = ["logs:GetLogEvents", "logs:FilterLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/ecs/skynet-${each.key}/*"]
  }

  statement {
    sid       = "ReadDeployParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/skynet/${each.key}/*"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  for_each = aws_iam_role.github_deploy

  name   = "deploy"
  role   = each.value.id
  policy = data.aws_iam_policy_document.github_deploy[each.key].json
}

# ---------------------------------------------------------------- preview role
# PR previews (SKY-164): .github/workflows/preview.yml runs scripts/preview.sh
# for pull requests from this repository (fork PRs get no OIDC token). The
# GitHub environment aws-preview must allow every branch, so anyone who can
# push a branch can run code with this role. It reaches only the preview
# pieces in the staging cluster:
#   - new revisions of the task definition families skynet-pr-*, tagged
#     Environment=preview (it cannot deregister or delete any revision),
#   - services pr-<n>-* and the migrate task of a preview family,
#   - the slot listener rules that Terraform made (tag Environment=preview,
#     modules/environment/preview.tf); it cannot create, delete or tag rules
#     or target groups,
#   - the skynet/preview-* image repositories and the slot parameters.
# It passes the staging backend, worker, frontend and migrate roles, so a
# preview reads the staging secrets. dbadmin and litellm roles stay out.
# Known limits: ModifyRule cannot limit the host or the action, so this role
# can point a slot rule at any host or staging target group (rules at 102-109
# match after the staging rules at 10 and 20). CreateService and UpdateService
# cannot limit the target group either, so a pr-* service can register its
# tasks in a staging target group.

locals {
  preview_env     = "staging"
  preview_cluster = "arn:aws:ecs:${var.region}:${local.account_id}:cluster/skynet-${local.preview_env}"
}

data "aws_iam_policy_document" "github_preview_trust" {
  count = var.enable_github_oidc ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_oidc_subject_prefix}:environment:aws-preview"]
    }
  }
}

resource "aws_iam_role" "github_preview" {
  count = var.enable_github_oidc ? 1 : 0

  name                 = "skynet-github-deploy-preview"
  description          = "Assumed by GitHub Actions (OIDC, environment aws-preview) to build and run PR previews in the staging cluster."
  assume_role_policy   = data.aws_iam_policy_document.github_preview_trust[0].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_preview" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPreviewImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchDeleteImage",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:ListImages",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for r in ["backend", "frontend"] : "arn:aws:ecr:${var.region}:${local.account_id}:repository/skynet/preview-${r}"]
  }

  statement {
    sid = "EcrBuildcacheRead"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["arn:aws:ecr:${var.region}:${local.account_id}:repository/skynet/buildcache-staging"]
  }

  statement {
    sid = "EcrPreviewBuildcache"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = ["arn:aws:ecr:${var.region}:${local.account_id}:repository/skynet/preview-buildcache"]
  }

  statement {
    sid       = "EcrStagingBackendRead"
    actions   = ["ecr:DescribeImages", "ecr:DescribeRepositories"]
    resources = ["arn:aws:ecr:${var.region}:${local.account_id}:repository/skynet/backend"]
  }

  statement {
    sid       = "EcsTaskDefinitionsRead"
    actions   = ["ecs:DescribeTaskDefinition", "ecs:ListTaskDefinitions"]
    resources = ["*"]
  }

  # preview.sh reads the Environment tag of the staging revisions with
  # describe-task-definition --include TAGS, which may need this action.
  statement {
    sid       = "EcsReadTaskDefinitionTags"
    actions   = ["ecs:ListTagsForResource"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-${local.preview_env}-*:*"]
  }

  statement {
    sid       = "EcsRegisterTaskDefinitions"
    actions   = ["ecs:RegisterTaskDefinition"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-pr-*:*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Environment"
      values   = ["preview"]
    }
  }

  # A preview service may run only a preview revision: without this, a
  # pr-<n> service could run the staging dbadmin or litellm revision.
  statement {
    sid       = "EcsPreviewServices"
    actions   = ["ecs:CreateService", "ecs:UpdateService"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:service/skynet-${local.preview_env}/pr-*"]

    condition {
      test     = "ArnLike"
      variable = "ecs:task-definition"
      values   = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-pr-*"]
    }
  }

  statement {
    sid       = "EcsDeleteServices"
    actions   = ["ecs:DeleteService", "ecs:DescribeServices"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:service/skynet-${local.preview_env}/pr-*"]
  }

  statement {
    sid       = "EcsRunMigrate"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-pr-*-migrate:*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [local.preview_cluster]
    }
  }

  statement {
    sid       = "EcsTasks"
    actions   = ["ecs:DescribeTasks"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task/skynet-${local.preview_env}/*"]
  }

  statement {
    sid     = "EcsTags"
    actions = ["ecs:TagResource"]
    resources = [
      "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-pr-*",
      "arn:aws:ecs:${var.region}:${local.account_id}:service/skynet-${local.preview_env}/pr-*",
      "arn:aws:ecs:${var.region}:${local.account_id}:task/skynet-${local.preview_env}/*",
    ]
  }

  statement {
    sid     = "PassTaskRoles"
    actions = ["iam:PassRole"]
    resources = flatten([
      for f in ["backend", "worker", "frontend", "migrate"] : [
        "arn:aws:iam::${local.account_id}:role/skynet-${local.preview_env}-${f}-exec",
        "arn:aws:iam::${local.account_id}:role/skynet-${local.preview_env}-${f}-task",
      ]
    ])

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid       = "ElbRead"
    actions   = ["elasticloadbalancing:Describe*"]
    resources = ["*"]
  }

  statement {
    sid       = "ElbPreviewRules"
    actions   = ["elasticloadbalancing:ModifyRule"]
    resources = ["arn:aws:elasticloadbalancing:${var.region}:${local.account_id}:listener-rule/app/skynet-${local.preview_env}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Environment"
      values   = ["preview"]
    }
  }

  statement {
    sid       = "ReadPreviewLogs"
    actions   = ["logs:GetLogEvents", "logs:FilterLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/ecs/skynet-${local.preview_env}/preview:*"]
  }

  statement {
    sid       = "ReadPreviewParameter"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/skynet/${local.preview_env}/preview"]
  }

  # The cap on live previews: a slot is claimed with PutParameter without
  # overwrite, which fails when another PR holds it.
  statement {
    sid = "PreviewSlots"
    actions = [
      "ssm:DeleteParameter",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:PutParameter",
    ]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/skynet/preview/slots/*"]
  }
}

resource "aws_iam_role_policy" "github_preview" {
  count = var.enable_github_oidc ? 1 : 0

  name   = "preview"
  role   = aws_iam_role.github_preview[0].id
  policy = data.aws_iam_policy_document.github_preview.json
}

# Preview cleanup role: the prune job of .github/workflows/preview-cleanup.yml
# deletes the task definitions of deleted previews. It trusts only workflow
# runs on main without a GitHub environment, because
# ecs:DeregisterTaskDefinition has no resource-level scope: this role could
# deregister any revision, so only reviewed code may assume it.
# DeleteTaskDefinitions is limited to the skynet-pr-* families.

data "aws_iam_policy_document" "github_preview_cleanup_trust" {
  count = var.enable_github_oidc ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_oidc_subject_prefix}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_preview_cleanup" {
  count = var.enable_github_oidc ? 1 : 0

  name                 = "skynet-github-preview-cleanup"
  description          = "Assumed by GitHub Actions (OIDC, main only) to delete the task definitions of deleted PR previews."
  assume_role_policy   = data.aws_iam_policy_document.github_preview_cleanup_trust[0].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_preview_cleanup" {
  statement {
    sid = "EcsTaskDefinitionsUnscoped"
    actions = [
      "ecs:DeregisterTaskDefinition",
      "ecs:ListTaskDefinitionFamilies",
      "ecs:ListTaskDefinitions",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "EcsDeleteTaskDefinitions"
    actions   = ["ecs:DeleteTaskDefinitions"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/skynet-pr-*:*"]
  }

  statement {
    sid       = "ReadPreviewSlots"
    actions   = ["ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/skynet/preview/slots/*"]
  }
}

resource "aws_iam_role_policy" "github_preview_cleanup" {
  count = var.enable_github_oidc ? 1 : 0

  name   = "preview-cleanup"
  role   = aws_iam_role.github_preview_cleanup[0].id
  policy = data.aws_iam_policy_document.github_preview_cleanup.json
}

# ---------------------------------------------------------------- terraform roles
# Plan role: pull requests from this repository (fork PRs get no OIDC token).
# Read-only. It reads the state bucket, and the state holds the generated
# database and Valkey passwords, so anyone who can push a branch here can
# read them through a PR job. That matches the trust GitHub already gives to
# people with write access.

data "aws_iam_policy_document" "github_plan_trust" {
  count = var.enable_github_oidc ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_oidc_subject_prefix}:pull_request"]
    }
  }
}

resource "aws_iam_role" "github_plan" {
  count = var.enable_github_oidc ? 1 : 0

  name                 = "skynet-github-terraform-plan"
  description          = "Assumed by the Terraform PR workflow (OIDC) to run terraform plan. Read-only."
  assume_role_policy   = data.aws_iam_policy_document.github_plan_trust[0].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "github_plan_readonly" {
  count = var.enable_github_oidc ? 1 : 0

  role       = aws_iam_role.github_plan[0].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# ReadOnlyAccess lacks GetSecretValue, which the plan needs to refresh the
# Terraform-generated secret versions. The plan runs with -lock=false, so it
# never writes the state lock file.
data "aws_iam_policy_document" "github_plan" {
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [local.state_bucket_arn, "${local.state_bucket_arn}/*"]
  }

  statement {
    sid     = "ReadGeneratedSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = flatten([
      for e in local.environments : [
        for s in ["app-generated", "litellm-generated", "db-admin"] :
        "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:skynet/${e}/${s}-*"
      ]
    ])
  }
}

resource "aws_iam_role_policy" "github_plan" {
  count = var.enable_github_oidc ? 1 : 0

  name   = "plan"
  role   = aws_iam_role.github_plan[0].id
  policy = data.aws_iam_policy_document.github_plan.json
}

# Apply role: only workflow runs on the staging and main branches. No
# workflow in this repository assumes it yet; CI only plans. Adding an apply
# job needs Gilad's approval (SKY-156 gate).
# This role is ADMIN-EQUIVALENT. PowerUserAccess plus IAM writes on the
# skynet-staging-* and skynet-production-* roles let it create a role with
# any policy and pass it to a task. Branch trust (staging, main) and branch
# protection are the real control. Its own policies do not let it change
# the skynet-github-* roles (itself, plan, deploy) or the OIDC provider
# directly. But it can create a skynet-<env>-* role with AdministratorAccess
# that trusts the apply role, assume that role, and then change them. No
# permissions boundary stops this. The global stack stays a manual apply by
# a person; that is a process rule, not an IAM control.

data "aws_iam_policy_document" "github_apply_trust" {
  count = var.enable_github_oidc ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "${var.github_oidc_subject_prefix}:ref:refs/heads/staging",
        "${var.github_oidc_subject_prefix}:ref:refs/heads/main",
      ]
    }
  }
}

resource "aws_iam_role" "github_apply" {
  count = var.enable_github_oidc ? 1 : 0

  name                 = "skynet-github-terraform-apply"
  description          = "Assumed by GitHub Actions (OIDC) on the staging and main branches to run terraform apply."
  assume_role_policy   = data.aws_iam_policy_document.github_apply_trust[0].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "github_apply_poweruser" {
  count = var.enable_github_oidc ? 1 : 0

  role       = aws_iam_role.github_apply[0].name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

data "aws_iam_policy_document" "github_apply_iam" {
  statement {
    sid     = "ManageEnvironmentRoles"
    actions = ["iam:*"]
    resources = [
      for e in local.environments : "arn:aws:iam::${local.account_id}:role/skynet-${e}-*"
    ]
  }

  statement {
    sid       = "ReadIam"
    actions   = ["iam:Get*", "iam:List*"]
    resources = ["*"]
  }

  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/aws-service-role/*"]
  }
}

resource "aws_iam_role_policy" "github_apply_iam" {
  count = var.enable_github_oidc ? 1 : 0

  name   = "iam"
  role   = aws_iam_role.github_apply[0].id
  policy = data.aws_iam_policy_document.github_apply_iam.json
}

output "ecr_repository_urls" {
  description = "ECR repository URL per image."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "github_deploy_role_arns" {
  description = "Deploy role ARN per environment. Set each as the AWS_DEPLOY_ROLE_ARN variable of the GitHub environment aws-<env> (empty while enable_github_oidc is false)."
  value       = { for k, r in aws_iam_role.github_deploy : k => r.arn }
}

output "github_preview_role_arn" {
  description = "PR preview role ARN. Set it as the AWS_DEPLOY_ROLE_ARN variable of the GitHub environment aws-preview (empty while enable_github_oidc is false)."
  value       = one(aws_iam_role.github_preview[*].arn)
}

output "github_preview_cleanup_role_arn" {
  description = "PR preview cleanup role ARN. Set it as the repository variable AWS_PREVIEW_CLEANUP_ROLE_ARN (empty while enable_github_oidc is false)."
  value       = one(aws_iam_role.github_preview_cleanup[*].arn)
}

output "github_plan_role_arn" {
  description = "Role ARN for the Terraform PR workflow (repository variable AWS_PLAN_ROLE_ARN)."
  value       = one(aws_iam_role.github_plan[*].arn)
}

output "github_apply_role_arn" {
  description = "Role ARN for a future apply workflow on staging and main. No workflow uses it yet."
  value       = one(aws_iam_role.github_apply[*].arn)
}

output "state_bucket_name" {
  description = "State bucket name (repository variable TF_STATE_BUCKET for the PR plan job)."
  value       = "skynet-tfstate-${local.account_id}-${var.region}"
}
