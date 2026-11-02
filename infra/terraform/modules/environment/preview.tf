# Shared pieces of the PR preview environments (SKY-164). scripts/preview.sh
# creates and deletes the per-PR pieces (ECS services, task definitions,
# database) at run time; they are not in Terraform state. The target groups
# and listener rules of the slots are here, so the preview role needs no
# right to create or delete them.
#
# Previews run in this environment's cluster with its secrets, task roles and
# security groups, behind its ALB, and share its RDS instance, Valkey and
# LiteLLM.

locals {
  preview_enabled = var.preview_domain != ""
  # Must match max_slots in scripts/preview.sh.
  preview_slots = local.preview_enabled ? 4 : 0
  preview_slot_targets = {
    for p in setproduct(range(1, local.preview_slots + 1), ["backend", "frontend"]) :
    "${p[0]}-${p[1]}" => { slot = p[0], key = p[1] }
  }
}

# ACM issues the wildcard with the same DNS validation record as the
# certificate for var.preview_domain itself, so no new record is needed when
# var.preview_domain is one of var.frontend_domains.
resource "aws_acm_certificate" "preview" {
  #checkov:skip=CKV2_AWS_71:Each PR preview gets its own host under var.preview_domain; one wildcard covers them all.
  count = local.preview_enabled ? 1 : 0

  domain_name       = "*.${var.preview_domain}"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "preview" {
  count = local.preview_enabled ? 1 : 0

  certificate_arn         = aws_acm_certificate.preview[0].arn
  validation_record_fqdns = [for o in aws_acm_certificate.preview[0].domain_validation_options : o.resource_record_name]
}

resource "aws_lb_listener_certificate" "preview" {
  count = local.preview_enabled && var.enable_https ? 1 : 0

  listener_arn    = aws_lb_listener.https[0].arn
  certificate_arn = aws_acm_certificate_validation.preview[0].certificate_arn
}

# One group for every preview. The environment's error alarms watch only the
# service groups, so preview errors do not page.
resource "aws_cloudwatch_log_group" "preview" {
  count = local.preview_enabled ? 1 : 0

  name              = "/ecs/${local.name}/preview"
  retention_in_days = 7
}

# One target group per slot and service. A preview service of the slot
# registers its tasks here; the group stays when the preview is deleted.
# The health check is faster than the service groups' one: a new preview task
# serves after two passes 5 seconds apart, not 15. Ten failures (50 seconds)
# mark it unhealthy, close to the 60 seconds of the earlier 4 x 15.
resource "aws_lb_target_group" "preview" {
  for_each = local.preview_slot_targets

  name                 = "${local.name}-pv${each.value.slot}-${each.value.key}"
  port                 = local.ports[each.value.key]
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.this.id
  deregistration_delay = 30

  health_check {
    path                = each.value.key == "backend" ? "/health" : "/api/version"
    matcher             = "200"
    interval            = 5
    timeout             = 4
    healthy_threshold   = 2
    unhealthy_threshold = 10
  }

  tags = {
    Environment = "preview"
  }
}

# Two rules per slot at priorities 100 + 2 * slot (api) and 101 + 2 * slot
# (app). They always forward to the slot's groups. While the slot is free,
# the host is a placeholder under .invalid that no request can carry.
# preview.sh changes only the host, and the Environment=preview tag lets the
# preview role modify these rules and no other rule.
resource "aws_lb_listener_rule" "preview" {
  for_each = local.preview_slot_targets

  listener_arn = local.routing_listener_arn
  priority     = 100 + 2 * each.value.slot + (each.value.key == "backend" ? 0 : 1)

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.preview[each.key].arn
  }

  condition {
    host_header {
      values = ["pr-slot-${each.value.slot}-${each.value.key}.invalid"]
    }
  }

  tags = {
    Environment = "preview"
  }

  lifecycle {
    ignore_changes = [condition]
  }
}

# Values that scripts/preview.sh needs. Not secret.
resource "aws_ssm_parameter" "preview" {
  count = local.preview_enabled ? 1 : 0

  name = "/skynet/${var.environment}/preview"
  type = "String"
  value = jsonencode({
    cluster          = aws_ecs_cluster.this.name
    vpc_id           = aws_vpc.this.id
    subnets          = local.tasks_public ? aws_subnet.public[*].id : aws_subnet.private[*].id
    assign_public_ip = local.tasks_public
    security_groups  = { for k in ["backend", "worker", "frontend", "migrate"] : k => aws_security_group.this[k].id }
    families         = { for k in ["backend", "worker", "frontend", "migrate"] : k => aws_ecs_task_definition.this[k].family }
    listener_arn     = local.routing_listener_arn
    domain           = var.preview_domain
    ports            = { backend = local.ports.backend, frontend = local.ports.frontend }
    log_group        = aws_cloudwatch_log_group.preview[0].name
    slots = {
      for k in range(1, local.preview_slots + 1) : tostring(k) => {
        for key in ["backend", "frontend"] : key => {
          target_group = aws_lb_target_group.preview["${k}-${key}"].arn
          rule         = aws_lb_listener_rule.preview["${k}-${key}"].arn
          placeholder  = "pr-slot-${k}-${key}.invalid"
        }
      }
    }
  })
}
