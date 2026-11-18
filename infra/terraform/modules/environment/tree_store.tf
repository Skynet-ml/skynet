# Tree and step log packs (SKY-710). The worker writes, reads, lists and
# compacts them. The backend may only delete objects: account deletion runs
# in the backend and purges the packs that only the deleted account used
# (SKY-733).

resource "aws_s3_bucket" "trees" {
  bucket        = "${local.name}-trees-${local.account_id}"
  force_destroy = var.environment != "production"
}

resource "aws_s3_bucket_public_access_block" "trees" {
  bucket                  = aws_s3_bucket.trees.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "trees" {
  bucket = aws_s3_bucket.trees.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "trees" {
  bucket = aws_s3_bucket.trees.id

  # Old packs are rarely read; Intelligent-Tiering moves them to cheaper tiers
  # with no retrieval fee.
  rule {
    id     = "intelligent-tiering"
    status = "Enabled"
    filter {}
    transition {
      days          = 0
      storage_class = "INTELLIGENT_TIERING"
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "trees_worker" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.trees.arn]
  }

  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.trees.arn}/*"]
  }
}

resource "aws_iam_role_policy" "trees_worker" {
  name   = "tree-packs"
  role   = aws_iam_role.task["worker"].id
  policy = data.aws_iam_policy_document.trees_worker.json
}

data "aws_iam_policy_document" "trees_backend" {
  statement {
    actions   = ["s3:DeleteObject"]
    resources = ["${aws_s3_bucket.trees.arn}/*"]
  }
}

resource "aws_iam_role_policy" "trees_backend" {
  name   = "tree-packs-delete"
  role   = aws_iam_role.task["backend"].id
  policy = data.aws_iam_policy_document.trees_backend.json
}
