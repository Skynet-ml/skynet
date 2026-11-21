# ECS cluster, task definitions and services.
#
# Image updates: scripts/deploy.sh registers a new revision of each family
# from the latest revision (so Terraform's environment and secrets carry over),
# swaps the image tag, runs the migrate task, then updates the services.
# The services therefore ignore task_definition changes after creation.
# A Terraform change to a task definition takes effect at the next deploy.

resource "aws_cloudwatch_log_group" "service" {
  for_each = local.families

  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "exec" {
  name              = "/ecs/${local.name}/exec-sessions"
  retention_in_days = 90
}

resource "aws_ecs_cluster" "this" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  configuration {
    execute_command_configuration {
      logging = "OVERRIDE"

      log_configuration {
        cloud_watch_log_group_name = aws_cloudwatch_log_group.exec.name
      }
    }
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name        = local.internal_zone
  description = "Private service names for Skynet ${var.environment} (replaces *.railway.internal)"
  vpc         = aws_vpc.this.id
}

resource "aws_service_discovery_service" "litellm" {
  name = "litellm"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.this.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
  }
}

locals {
  image = {
    backend  = "${var.ecr_repository_urls["backend"]}:${var.image_tag}"
    frontend = "${var.ecr_repository_urls["frontend"]}:${var.image_tag}-${var.environment}"
    litellm  = "${var.ecr_repository_urls["litellm"]}:${var.image_tag}"
    # Pinned by digest (the multi-arch index of postgres:18-alpine, 18.6,
    # 2026-10-21): this task holds the RDS master password.
    dbadmin = "public.ecr.aws/docker/library/postgres:18-alpine@sha256:83148c0bc09eac4d598b70a61e1404d0212b76c95c504bfd6c81a4c59ec91792"
  }

  app_common_env = {
    APP_ENVIRONMENT               = var.environment
    APP_GIT_SHA                   = var.image_tag
    ALLOWED_ORIGINS               = join(",", local.site_origins)
    APP_PUBLIC_URL                = local.site_url
    FRONTEND_URL                  = local.site_url
    LITELLM_PROXY_URL             = local.litellm_url
    DB_PGBOUNCER_TRANSACTION_MODE = "false"
  }

  container_env = {
    backend = merge(local.app_common_env, {
      APP_SERVICE_NAME = "backend"
      PORT             = tostring(local.ports.backend)
      # Uvicorn reads FORWARDED_ALLOW_IPS from the environment (and PR #788
      # passes it explicitly): trust X-Forwarded-* only from the ALB subnets.
      FORWARDED_ALLOW_IPS = join(",", local.public_cidrs)
      WEBAUTHN_RP_ID      = var.frontend_domains[0]
      WEBAUTHN_ORIGINS    = join(",", local.site_origins)
      # Jobs run in the dedicated worker service.
      WORKER_ENABLED = "false"
      # Resource URL of the remote MCP server (SKY-229); empty turns it off.
      MCP_RESOURCE_URL = var.mcp_domain == "" ? "" : "https://${var.mcp_domain}/mcp"
      # Account deletion purges the account's tree packs (SKY-733).
      SKYNET_TREE_BUCKET = aws_s3_bucket.trees.id
      # Account deletion deletes the account's user image repository (SKY-894).
      USER_IMAGES_REPOSITORY_PREFIX_URI = local.user_images_prefix_uri
    })
    worker = merge(local.app_common_env, {
      APP_SERVICE_NAME                  = "worker"
      SERVICE_ROLE                      = "worker"
      SKYNET_TREE_BUCKET                = aws_s3_bucket.trees.id
      USER_IMAGES_REPOSITORY_PREFIX_URI = local.user_images_prefix_uri
    })
    migrate = merge(local.app_common_env, {
      APP_SERVICE_NAME = "migrate"
    })
    frontend = {
      APP_ENVIRONMENT      = var.environment
      APP_GIT_SHA          = var.image_tag
      APP_SERVICE_NAME     = "frontend"
      PORT                 = tostring(local.ports.frontend)
      HOSTNAME             = "0.0.0.0"
      API_URL              = local.api_url
      NEXT_PUBLIC_API_URL  = local.api_url
      NEXT_PUBLIC_SITE_URL = local.site_url
      AUTH_URL             = local.site_url
    }
    litellm = {
      APP_ENVIRONMENT = var.environment
      REDIS_HOST      = local.redis_endpoint
      REDIS_PORT      = "6379"
      # ElastiCache has in-transit encryption on. litellm's Redis client
      # reads REDIS_SSL from the environment.
      REDIS_SSL = "True"
    }
    dbadmin = {}
  }

  container_secrets = {
    backend = concat(
      [for k in local.backend_secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.manual["app"].arn}:${k}::" }],
      [for k in ["REMOTE_DB_URL", "REDIS_URL"] : { name = k, valueFrom = "${aws_secretsmanager_secret.app_generated.arn}:${k}::" }],
    )
    worker = concat(
      [for k in local.worker_secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.manual["app"].arn}:${k}::" }],
      [for k in ["REMOTE_DB_URL", "REDIS_URL"] : { name = k, valueFrom = "${aws_secretsmanager_secret.app_generated.arn}:${k}::" }],
    )
    migrate = concat(
      [for k in local.backend_secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.manual["app"].arn}:${k}::" }],
      [for k in ["REMOTE_DB_URL", "REDIS_URL"] : { name = k, valueFrom = "${aws_secretsmanager_secret.app_generated.arn}:${k}::" }],
    )
    frontend = [for k in local.frontend_secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.manual["frontend"].arn}:${k}::" }]
    litellm = concat(
      [for k in local.litellm_secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.manual["litellm"].arn}:${k}::" }],
      [for k in ["LITELLM_DATABASE_URL", "REDIS_PASSWORD"] : { name = k, valueFrom = "${aws_secretsmanager_secret.litellm_generated.arn}:${k}::" }],
    )
    dbadmin = concat(
      [for k in ["PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE"] : { name = k, valueFrom = "${aws_secretsmanager_secret.db_admin.arn}:${k}::" }],
      [{ name = "APP_DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.app_generated.arn}:APP_DB_PASSWORD::" }],
      [{ name = "LITELLM_DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.litellm_generated.arn}:LITELLM_DB_PASSWORD::" }],
    )
  }

  task = {
    backend = {
      image        = local.image.backend
      cpu          = var.services["backend"].cpu
      memory       = var.services["backend"].memory
      port         = local.ports.backend
      command      = null
      stop_timeout = 120
      health_check = null
    }
    worker = {
      image   = local.image.backend
      cpu     = var.services["worker"].cpu
      memory  = var.services["worker"].memory
      port    = null
      command = null
      # Fargate's maximum (Railway drained the worker for 300 s). Running
      # jobs get 120 s after SIGTERM to finish or hand off.
      stop_timeout = 120
      health_check = null
    }
    migrate = {
      image        = local.image.backend
      cpu          = var.migrate_task.cpu
      memory       = var.migrate_task.memory
      port         = null
      command      = ["python", "manage.py", "migrate"]
      stop_timeout = 30
      health_check = null
    }
    frontend = {
      image        = local.image.frontend
      cpu          = var.services["frontend"].cpu
      memory       = var.services["frontend"].memory
      port         = local.ports.frontend
      command      = null
      stop_timeout = 60
      health_check = null
    }
    litellm = {
      image  = local.image.litellm
      cpu    = var.services["litellm"].cpu
      memory = var.services["litellm"].memory
      port   = local.ports.litellm
      # Same bind as the Dockerfile CMD, kept explicit: on "::" uvicorn sets
      # IPV6_V6ONLY, so IPv4 callers (the health check on 127.0.0.1 and the
      # backend via Cloud Map) are refused.
      command      = ["--config", "/app/config.yaml", "--port", "4000", "--host", "0.0.0.0"]
      stop_timeout = 120
      health_check = {
        command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=5)"]
        interval    = 30
        timeout     = 10
        retries     = 3
        startPeriod = 120
      }
    }
    # On-demand admin task: no service. scripts/db-tunnel.sh starts it and
    # forwards a local port to RDS through ECS Exec. It stops itself after 4 h.
    dbadmin = {
      image        = local.image.dbadmin
      cpu          = 512
      memory       = 1024
      port         = null
      command      = ["sleep", "14400"]
      stop_timeout = 10
      health_check = null
    }
  }
}

resource "aws_ecs_task_definition" "this" {
  for_each = local.task

  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution[each.key].arn
  task_role_arn            = aws_iam_role.task[each.key].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    merge(
      {
        name        = each.key
        image       = each.value.image
        essential   = true
        stopTimeout = each.value.stop_timeout
        environment = [for k, v in local.container_env[each.key] : { name = k, value = v }]
        secrets     = local.container_secrets[each.key]
        portMappings = each.value.port == null ? [] : [{
          containerPort = each.value.port
          protocol      = "tcp"
        }]
        linuxParameters = { initProcessEnabled = true }
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            awslogs-group         = aws_cloudwatch_log_group.service[each.key].name
            awslogs-region        = var.region
            awslogs-stream-prefix = each.key
          }
        }
      },
      each.value.command == null ? {} : { command = each.value.command },
      each.value.health_check == null ? {} : { healthCheck = each.value.health_check },
    )
  ])
}

locals {
  services = {
    backend = {
      target_group_arn = aws_lb_target_group.backend.arn
      port             = local.ports.backend
      registry_arn     = null
    }
    worker = {
      target_group_arn = null
      port             = null
      registry_arn     = null
    }
    frontend = {
      target_group_arn = aws_lb_target_group.frontend.arn
      port             = local.ports.frontend
      registry_arn     = null
    }
    litellm = {
      target_group_arn = null
      port             = null
      registry_arn     = aws_service_discovery_service.litellm.arn
    }
  }
}

resource "aws_ecs_service" "this" {
  for_each = var.services_enabled ? local.services : {}

  name             = each.key
  cluster          = aws_ecs_cluster.this.id
  task_definition  = aws_ecs_task_definition.this[each.key].arn
  desired_count    = var.services[each.key].desired_count
  platform_version = "LATEST"
  # ECS needs a new deployment to move running tasks between FARGATE and
  # FARGATE_SPOT; without it a spot change waits for the next deploy.
  force_new_deployment   = true
  enable_execute_command = true
  propagate_tags         = "SERVICE"
  wait_for_steady_state  = false

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = each.value.target_group_arn == null ? null : 180

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # A capacity provider strategy (not launch_type) lets a service move between
  # on-demand and Spot in place.
  capacity_provider_strategy {
    capacity_provider = var.services[each.key].spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets          = local.tasks_public ? aws_subnet.public[*].id : aws_subnet.private[*].id
    security_groups  = [aws_security_group.this[each.key].id]
    assign_public_ip = local.tasks_public
  }

  dynamic "load_balancer" {
    for_each = each.value.target_group_arn == null ? [] : [1]
    content {
      target_group_arn = each.value.target_group_arn
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  dynamic "service_registries" {
    for_each = each.value.registry_arn == null ? [] : [1]
    content {
      registry_arn = each.value.registry_arn
    }
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [
    aws_lb_listener_rule.api,
    aws_lb_listener_rule.frontend,
    aws_iam_role_policy.execution_secrets,
  ]
}

# CPU target tracking for the HTTP services. The minimum is the configured
# desired count; the maximum is three times that.
resource "aws_appautoscaling_target" "this" {
  for_each = var.services_enabled ? toset(["backend", "frontend"]) : toset([])

  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.this[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.services[each.key].desired_count
  max_capacity       = var.services[each.key].desired_count * 3
}

resource "aws_appautoscaling_policy" "cpu" {
  for_each = aws_appautoscaling_target.this

  name               = "${local.name}-${each.key}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = each.value.service_namespace
  resource_id        = each.value.resource_id
  scalable_dimension = each.value.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# Values that scripts/deploy.sh needs. Not secret.
resource "aws_ssm_parameter" "deploy" {
  name = "/skynet/${var.environment}/deploy"
  type = "String"
  value = jsonencode({
    cluster          = aws_ecs_cluster.this.name
    subnets          = local.tasks_public ? aws_subnet.public[*].id : aws_subnet.private[*].id
    assign_public_ip = local.tasks_public
    security_groups  = { for k in ["migrate", "dbadmin"] : k => aws_security_group.this[k].id }
    families         = { for k, v in aws_ecs_task_definition.this : k => v.family }
    services         = keys(local.services)
    ecr              = var.ecr_repository_urls
    migrate_log      = aws_cloudwatch_log_group.service["migrate"].name
    frontend_tag_sfx = "-${var.environment}"
  })
}
