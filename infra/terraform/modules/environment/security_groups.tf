locals {
  # Task security groups that may reach each data store / service.
  # "migrate" runs manage.py migrate; "dbadmin" is the on-demand restore and
  # port-forward task (scripts/db-tunnel.sh).
  sg_names = toset(["alb", "backend", "worker", "frontend", "litellm", "migrate", "dbadmin", "postgres", "valkey"])

  postgres_clients = ["backend", "worker", "litellm", "migrate", "dbadmin"]
  valkey_clients   = ["backend", "worker", "litellm"]
  litellm_clients  = ["backend", "worker"]

  # Tasks that need outbound internet (LLM providers, Stripe, OAuth, SMTP,
  # Sentry, ECR/Secrets Manager/Logs over NAT).
  egress_anywhere = toset(["backend", "worker", "frontend", "litellm", "migrate", "dbadmin"])
}

resource "aws_security_group" "this" {
  for_each = local.sg_names

  name        = "${local.name}-${each.key}"
  description = "Skynet ${var.environment} ${each.key}"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-${each.key}" }

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------- ALB

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.this["alb"].id
  description       = "HTTPS from the internet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.this["alb"].id
  description       = "HTTP from the internet (redirects to HTTPS)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_backend" {
  security_group_id            = aws_security_group.this["alb"].id
  description                  = "To backend tasks"
  referenced_security_group_id = aws_security_group.this["backend"].id
  ip_protocol                  = "tcp"
  from_port                    = local.ports.backend
  to_port                      = local.ports.backend
}

resource "aws_vpc_security_group_egress_rule" "alb_to_frontend" {
  security_group_id            = aws_security_group.this["alb"].id
  description                  = "To frontend tasks"
  referenced_security_group_id = aws_security_group.this["frontend"].id
  ip_protocol                  = "tcp"
  from_port                    = local.ports.frontend
  to_port                      = local.ports.frontend
}

# ---------------------------------------------------------------- tasks

resource "aws_vpc_security_group_ingress_rule" "backend_from_alb" {
  security_group_id            = aws_security_group.this["backend"].id
  description                  = "From the ALB"
  referenced_security_group_id = aws_security_group.this["alb"].id
  ip_protocol                  = "tcp"
  from_port                    = local.ports.backend
  to_port                      = local.ports.backend
}

resource "aws_vpc_security_group_ingress_rule" "frontend_from_alb" {
  security_group_id            = aws_security_group.this["frontend"].id
  description                  = "From the ALB"
  referenced_security_group_id = aws_security_group.this["alb"].id
  ip_protocol                  = "tcp"
  from_port                    = local.ports.frontend
  to_port                      = local.ports.frontend
}

resource "aws_vpc_security_group_ingress_rule" "litellm" {
  for_each = toset(local.litellm_clients)

  security_group_id            = aws_security_group.this["litellm"].id
  description                  = "From ${each.key}"
  referenced_security_group_id = aws_security_group.this[each.key].id
  ip_protocol                  = "tcp"
  from_port                    = local.ports.litellm
  to_port                      = local.ports.litellm
}

# The tasks call LLM providers, OAuth, Stripe and user-supplied webhooks on
# hosts and ports that are not known ahead of time.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "tasks_anywhere" {
  for_each = local.egress_anywhere

  security_group_id = aws_security_group.this[each.key].id
  description       = "Outbound to providers and AWS APIs"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------------------------------------------------------------- data stores

resource "aws_vpc_security_group_ingress_rule" "postgres" {
  for_each = toset(local.postgres_clients)

  security_group_id            = aws_security_group.this["postgres"].id
  description                  = "PostgreSQL from ${each.key}"
  referenced_security_group_id = aws_security_group.this[each.key].id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "valkey" {
  for_each = toset(local.valkey_clients)

  security_group_id            = aws_security_group.this["valkey"].id
  description                  = "Valkey from ${each.key}"
  referenced_security_group_id = aws_security_group.this[each.key].id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}
