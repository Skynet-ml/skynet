# One public ALB per environment. Host rules route the API host to the backend
# and every other configured host to the frontend.

resource "aws_acm_certificate" "this" {
  domain_name               = var.frontend_domains[0]
  subject_alternative_names = distinct(concat(slice(var.frontend_domains, 1, length(var.frontend_domains)), [var.api_domain]))
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_s3_bucket" "alb_logs" {
  bucket        = "${local.name}-alb-logs-${local.account_id}"
  force_destroy = var.environment != "production"
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket                  = aws_s3_bucket.alb_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  # ALB access logs support SSE-S3 only, not SSE-KMS.
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "alb_logs" {
  statement {
    sid       = "AlbLogDelivery"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.alb_logs.arn}/alb/AWSLogs/${local.account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }
  }

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.alb_logs.arn,
      "${aws_s3_bucket.alb_logs.arn}/*",
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

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  policy = data.aws_iam_policy_document.alb_logs.json

  depends_on = [aws_s3_bucket_public_access_block.alb_logs]
}

# The ALB is the public entry point; WAF and security groups limit it.
#trivy:ignore:AWS-0053
resource "aws_lb" "this" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.this["alb"].id]
  subnets                    = aws_subnet.public[*].id
  idle_timeout               = var.alb_idle_timeout
  drop_invalid_header_fields = true
  enable_deletion_protection = var.environment == "production"

  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "alb"
    enabled = true
  }

  depends_on = [aws_s3_bucket_policy.alb_logs]
}

resource "aws_lb_target_group" "backend" {
  name                 = "${local.name}-backend"
  port                 = local.ports.backend
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.this.id
  deregistration_delay = 120

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 15
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 4
  }
}

resource "aws_lb_target_group" "frontend" {
  name                 = "${local.name}-frontend"
  port                 = local.ports.frontend
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.this.id
  deregistration_delay = 60

  health_check {
    path                = "/api/version"
    matcher             = "200"
    interval            = 15
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 4
  }
}

# Port 80 only redirects to HTTPS. It forwards plain HTTP only while
# enable_https is false, before the ACM certificate is validated.
#trivy:ignore:AWS-0054
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = var.enable_https ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  # Before the certificate is issued, the host rules below sit on this
  # listener so the ECS services can attach to their target groups and be
  # smoke-tested with a Host header against the ALB DNS name.
  dynamic "default_action" {
    for_each = var.enable_https ? [] : [1]
    content {
      type = "fixed-response"
      fixed_response {
        content_type = "text/plain"
        message_body = "Not found"
        status_code  = "404"
      }
    }
  }
}

locals {
  routing_listener_arn = var.enable_https ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
}

resource "aws_lb_listener" "https" {
  count = var.enable_https ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.this.arn

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Not found"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = local.routing_listener_arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  condition {
    host_header {
      values = compact([var.api_domain, var.mcp_domain])
    }
  }
}

resource "aws_lb_listener_rule" "frontend" {
  listener_arn = local.routing_listener_arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }

  condition {
    host_header {
      values = var.frontend_domains
    }
  }
}

# Remote MCP server host (SKY-229). The api rule above sends it to the backend,
# which serves the MCP endpoint by host. Its own certificate keeps the main
# certificate unchanged. First apply: target aws_acm_certificate.mcp, create
# the records of acm_validation_records, then apply the rest.
locals {
  wildcard_enabled        = var.wildcard_domain != ""
  mcp_on_wildcard         = local.wildcard_enabled && can(regex("^[^.]+\\.${replace(var.wildcard_domain, ".", "\\.")}$", var.mcp_domain))
  mcp_certificate_enabled = var.mcp_domain != "" && !local.mcp_on_wildcard
}

# mcp.staging.skynetml.com uses this wildcard certificate. ACM issues the
# wildcard with the same DNS validation record as the certificate for
# var.wildcard_domain itself.
resource "aws_acm_certificate" "wildcard" {
  #checkov:skip=CKV2_AWS_71:The remote MCP host directly under var.wildcard_domain uses this wildcard.
  count = local.wildcard_enabled ? 1 : 0

  domain_name       = "*.${var.wildcard_domain}"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "wildcard" {
  count = local.wildcard_enabled ? 1 : 0

  certificate_arn         = aws_acm_certificate.wildcard[0].arn
  validation_record_fqdns = [for o in aws_acm_certificate.wildcard[0].domain_validation_options : o.resource_record_name]
}

resource "aws_lb_listener_certificate" "wildcard" {
  count = local.wildcard_enabled && var.enable_https ? 1 : 0

  listener_arn    = aws_lb_listener.https[0].arn
  certificate_arn = aws_acm_certificate_validation.wildcard[0].certificate_arn
}

moved {
  from = aws_acm_certificate.preview
  to   = aws_acm_certificate.wildcard
}

moved {
  from = aws_acm_certificate_validation.preview
  to   = aws_acm_certificate_validation.wildcard
}

moved {
  from = aws_lb_listener_certificate.preview
  to   = aws_lb_listener_certificate.wildcard
}

resource "aws_acm_certificate" "mcp" {
  count = local.mcp_certificate_enabled ? 1 : 0

  domain_name       = var.mcp_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "mcp" {
  count = local.mcp_certificate_enabled && var.enable_https ? 1 : 0

  certificate_arn         = aws_acm_certificate.mcp[0].arn
  validation_record_fqdns = [for o in aws_acm_certificate.mcp[0].domain_validation_options : o.resource_record_name]
}

resource "aws_lb_listener_certificate" "mcp" {
  count = local.mcp_certificate_enabled && var.enable_https ? 1 : 0

  listener_arn    = aws_lb_listener.https[0].arn
  certificate_arn = aws_acm_certificate_validation.mcp[0].certificate_arn
}
