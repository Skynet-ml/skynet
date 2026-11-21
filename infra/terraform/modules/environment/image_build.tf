# User images (SKY-737, SKY-888). A power user can bring a Dockerfile for the
# candidate box or the metric box. The worker zips the build context into the
# source bucket and starts the CodeBuild project with an inline buildspec
# (backend/core/sandbox/image_build.py). CodeBuild builds with buildx, keeps a
# registry cache for each owner (cache-<scope> tags) and pushes the image to
# the user-image repository. The worker then reads the build log, checks the
# digest and pins it for the run.

locals {
  image_build_name = "${local.name}-image-build"
  # Built from the name, so the trust policy needs no reference to the project.
  image_build_project_arn = "arn:aws:codebuild:${var.region}:${local.account_id}:project/${local.image_build_name}"
}

# ---------------------------------------------------------------- registry

resource "aws_ecr_repository" "user_images" {
  #checkov:skip=CKV_AWS_51:Image tags are immutable. Only the cache-* build cache tags move, and buildx must rewrite them.
  name = "skynet/user-images-${var.environment}"
  # Image tags are <input hash>-<build token>, so each one is written once.
  # Only the per-owner build cache tags move.
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"
  force_delete         = var.environment != "production"

  image_tag_mutability_exclusion_filter {
    filter      = "cache-*"
    filter_type = "WILDCARD"
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "user_images" {
  repository = aws_ecr_repository.user_images.name

  # A run pins its image by digest and a later run reuses it by input hash,
  # so the backend checks that a digest still exists before it reuses one.
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
        description  = "Delete build caches 14 days after their last push"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["cache-"]
          countType     = "sinceImagePushed"
          countUnit     = "days"
          countNumber   = 14
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 3
        description  = "Delete user images 90 days after their push"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "sinceImagePushed"
          countUnit      = "days"
          countNumber    = 90
        }
        action = { type = "expire" }
      },
    ]
  })
}

# ---------------------------------------------------------------- build source

resource "aws_s3_bucket" "image_build_source" {
  bucket        = "${local.image_build_name}-src-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "image_build_source" {
  bucket                  = aws_s3_bucket.image_build_source.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "image_build_source" {
  bucket = aws_s3_bucket.image_build_source.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "image_build_source_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.image_build_source.arn,
      "${aws_s3_bucket.image_build_source.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "image_build_source" {
  bucket = aws_s3_bucket.image_build_source.id
  policy = data.aws_iam_policy_document.image_build_source_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.image_build_source]
}

# A build reads its source once, at the start. The zipped contexts are user
# code, so they do not stay longer than a day.
resource "aws_s3_bucket_lifecycle_configuration" "image_build_source" {
  bucket = aws_s3_bucket.image_build_source.id

  rule {
    id     = "expire-sources"
    status = "Enabled"
    filter {}
    expiration {
      days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# ---------------------------------------------------------------- codebuild

resource "aws_cloudwatch_log_group" "image_build" {
  name              = "/aws/codebuild/${local.image_build_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "image_build_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [local.image_build_project_arn]
    }
  }
}

resource "aws_iam_role" "image_build" {
  name               = local.image_build_name
  assume_role_policy = data.aws_iam_policy_document.image_build_trust.json
}

data "aws_iam_policy_document" "image_build" {
  statement {
    sid       = "WriteBuildLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.image_build.arn}:*"]
  }

  statement {
    sid       = "ReadSourceBucket"
    actions   = ["s3:GetBucketAcl", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.image_build_source.arn]
  }

  statement {
    sid       = "ReadSource"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.image_build_source.arn}/*"]
  }

  # GetAuthorizationToken has no resource-level permission.
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushUserImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.user_images.arn]
  }
}

resource "aws_iam_role_policy" "image_build" {
  name   = "image-build"
  role   = aws_iam_role.image_build.id
  policy = data.aws_iam_policy_document.image_build.json
}

# The worker starts every build with a source and buildspec override, so the
# project's own source is only a guard that fails a build started without one.
resource "aws_codebuild_project" "image_build" {
  #checkov:skip=CKV_AWS_147:The artifacts are off and ECR encrypts the images with KMS; a CMK costs more (see .checkov.yaml).
  #checkov:skip=CKV_AWS_316:A Docker image build needs privileged mode; each build runs alone in a fresh CodeBuild container.
  name         = local.image_build_name
  description  = "Builds user images for the candidate box and the metric box (SKY-737)."
  service_role = aws_iam_role.image_build.arn
  # The worker sets the configured time limit on each build.
  build_timeout  = 60
  queued_timeout = 60

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = true
  }

  source {
    type      = "NO_SOURCE"
    buildspec = <<-EOT
      version: 0.2
      phases:
        build:
          commands:
            - echo "Start this project with a source and buildspec override." && exit 1
    EOT
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.image_build.name
      status     = "ENABLED"
    }

    s3_logs {
      status = "DISABLED"
    }
  }

  depends_on = [aws_iam_role_policy.image_build]
}

# ---------------------------------------------------------------- worker access

data "aws_iam_policy_document" "image_build_worker" {
  statement {
    sid       = "RunImageBuilds"
    actions   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds", "codebuild:StopBuild"]
    resources = [aws_codebuild_project.image_build.arn]
  }

  statement {
    sid       = "ReadImageBuildLogs"
    actions   = ["logs:GetLogEvents"]
    resources = ["${aws_cloudwatch_log_group.image_build.arn}:*"]
  }

  statement {
    sid       = "UploadBuildSource"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.image_build_source.arn}/*"]
  }

  # The worker checks a digest before it pins it, and pulls the image to copy
  # it into a provider's registry.
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "ReadUserImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.user_images.arn]
  }
}

resource "aws_iam_role_policy" "image_build_worker" {
  name   = "image-build"
  role   = aws_iam_role.task["worker"].id
  policy = data.aws_iam_policy_document.image_build_worker.json
}
