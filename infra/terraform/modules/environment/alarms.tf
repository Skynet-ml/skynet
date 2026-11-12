# Basic alarms. All go to one SNS topic per environment.

# CloudWatch and RDS cannot publish to a topic under the AWS managed SNS key
# (its policy cannot grant them the key), so the topic uses its own key.
resource "aws_kms_key" "alarms" {
  description             = "${local.name} alarm topic"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "AlarmPublishers"
        Effect = "Allow"
        Principal = {
          Service = ["cloudwatch.amazonaws.com", "events.rds.amazonaws.com"]
        }
        Action   = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_kms_alias" "alarms" {
  name          = "alias/${local.name}-alarms"
  target_key_id = aws_kms_key.alarms.key_id
}

resource "aws_sns_topic" "alarms" {
  name              = "${local.name}-alarms"
  kms_master_key_id = aws_kms_key.alarms.arn
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarm_actions = [aws_sns_topic.alarms.arn]
  alb_suffix    = aws_lb.this.arn_suffix

  target_groups = {
    backend  = aws_lb_target_group.backend.arn_suffix
    frontend = aws_lb_target_group.frontend.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  for_each = local.target_groups

  alarm_name          = "${local.name}-${each.key}-5xx"
  alarm_description   = "${each.key} returns 5xx responses."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 20
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  dimensions = {
    LoadBalancer = local.alb_suffix
    TargetGroup  = each.value
  }
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  for_each = local.target_groups

  alarm_name          = "${local.name}-${each.key}-unhealthy"
  alarm_description   = "${each.key} has fewer healthy targets than tasks for 3 minutes."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  dimensions = {
    LoadBalancer = local.alb_suffix
    TargetGroup  = each.value
  }
}

resource "aws_cloudwatch_metric_alarm" "backend_latency" {
  alarm_name          = "${local.name}-backend-p95-latency"
  alarm_description   = "Backend p95 response time is above 5 s. Streaming agent turns are long by design; tune if noisy."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 3
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = {
    LoadBalancer = local.alb_suffix
    TargetGroup  = local.target_groups.backend
  }
}

resource "aws_cloudwatch_metric_alarm" "service_cpu" {
  for_each = aws_ecs_service.this

  alarm_name          = "${local.name}-${each.key}-cpu"
  alarm_description   = "${each.key} average CPU is above 85 % for 15 minutes."
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = {
    ClusterName = aws_ecs_cluster.this.name
    ServiceName = aws_ecs_service.this[each.key].name
  }
}

resource "aws_cloudwatch_metric_alarm" "service_memory" {
  for_each = aws_ecs_service.this

  alarm_name          = "${local.name}-${each.key}-memory"
  alarm_description   = "${each.key} average memory is above 85 % for 15 minutes."
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = {
    ClusterName = aws_ecs_cluster.this.name
    ServiceName = aws_ecs_service.this[each.key].name
  }
}

# Fires when a service runs no tasks (crash loop, failed start, image pull
# failure, scale to 0). A fixed floor of 1, not "running - desired": a scale
# to 0 lowers desired too. Container Insights (enabled) sends no data while a
# service has no tasks. FILL turns each missing minute into 0: with
# treat_missing_data alone, CloudWatch keeps using older real points while
# its look-back range holds enough of them, which delays the alarm.
# 3 of 5 one-minute points: ALARM about 3 to 5 minutes after the outage.
resource "aws_cloudwatch_metric_alarm" "service_running" {
  for_each = { for k, v in aws_ecs_service.this : k => v if var.services[k].desired_count > 0 }

  alarm_name          = "${local.name}-${each.key}-tasks-missing"
  alarm_description   = "${each.key} ran no tasks in 3 of the last 5 minutes."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  metric_query {
    id          = "filled"
    expression  = "FILL(running, 0)"
    label       = "running tasks, missing as 0"
    return_data = true
  }

  metric_query {
    id = "running"
    metric {
      namespace   = "ECS/ContainerInsights"
      metric_name = "RunningTaskCount"
      period      = 60
      stat        = "Minimum"
      dimensions = {
        ClusterName = aws_ecs_cluster.this.name
        ServiceName = each.value.name
      }
    }
  }
}

# Fires when a service runs fewer tasks than desired (lost or failing tasks
# of a multi-task service). Desired is the live ECS value: the service
# ignores desired_count changes after create. 10 of 10 one-minute points,
# because CPU target tracking can scale out in several steps (one task per
# step when CPU is just above target, 60 s cooldown), so a scale-out to 3x
# desired can keep running below desired for up to about 7 minutes while
# the tasks start. A service with no tasks is the alarm above, which stays
# within 5 minutes.
resource "aws_cloudwatch_metric_alarm" "service_below_desired" {
  for_each = aws_cloudwatch_metric_alarm.service_running

  alarm_name          = "${local.name}-${each.key}-tasks-below-desired"
  alarm_description   = "${each.key} ran fewer tasks than desired for 10 minutes."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 10
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  metric_query {
    id          = "gap"
    expression  = "running - desired"
    label       = "running minus desired"
    return_data = true
  }

  metric_query {
    id = "running"
    metric {
      namespace   = "ECS/ContainerInsights"
      metric_name = "RunningTaskCount"
      period      = 60
      stat        = "Minimum"
      dimensions = {
        ClusterName = aws_ecs_cluster.this.name
        ServiceName = aws_ecs_service.this[each.key].name
      }
    }
  }

  metric_query {
    id = "desired"
    metric {
      namespace   = "ECS/ContainerInsights"
      metric_name = "DesiredTaskCount"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        ClusterName = aws_ecs_cluster.this.name
        ServiceName = aws_ecs_service.this[each.key].name
      }
    }
  }
}

# ------------------------------------------------------------ task crashes
# ECS replaces a crashed task in about a minute, too fast for the
# tasks-missing alarm. These rules match each stopped service task whose stop
# is a crash: the essential container exited, the task failed to start, a
# health check failed, or someone stopped it by hand. Deploys and scale-in
# stop tasks with ServiceSchedulerInitiated and a "Scaling activity" reason,
# so they do not match. The exact service group leaves out the migrate task.
resource "aws_cloudwatch_event_rule" "task_crashed" {
  for_each = aws_cloudwatch_metric_alarm.service_running

  name        = "${local.name}-${each.key}-task-crashed"
  description = "A ${each.key} task stopped because of a crash."

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.this.arn]
      group      = ["service:${aws_ecs_service.this[each.key].name}"]
      lastStatus = ["STOPPED"]
      "$or" = [
        { stopCode = ["EssentialContainerExited", "TaskFailedToStart", "UserInitiated"] },
        { stoppedReason = [{ prefix = "Task failed" }] },
      ]
    }
  })
}

# The log keeps each stop event with its reason. The alarm reads the rule's
# own MatchedEvents metric, so it does not depend on log delivery.
resource "aws_cloudwatch_log_group" "task_crashes" {
  name              = "/aws/events/${local.name}-task-crashes"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_resource_policy" "task_crashes" {
  policy_name = "${local.name}-task-crashes"

  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.task_crashes.arn}:*"
      Condition = {
        ArnEquals = { "aws:SourceArn" = [for r in aws_cloudwatch_event_rule.task_crashed : r.arn] }
      }
    }]
  })
}

resource "aws_cloudwatch_event_target" "task_crashed" {
  for_each = aws_cloudwatch_event_rule.task_crashed

  rule = each.value.name
  arn  = aws_cloudwatch_log_group.task_crashes.arn
}

# 1 of 5 one-minute points: the alarm fires about 1 to 2 minutes after a
# crash and returns to OK 5 minutes after the last one, so a crash loop sends
# one ALARM email, not one per crash.
resource "aws_cloudwatch_metric_alarm" "task_crashed" {
  for_each = aws_cloudwatch_event_rule.task_crashed

  alarm_name          = "${local.name}-${each.key}-task-crashed"
  alarm_description   = "A ${each.key} task crashed in the last 5 minutes. The stop reason is in the ${aws_cloudwatch_log_group.task_crashes.name} log group."
  namespace           = "AWS/Events"
  metric_name         = "MatchedEvents"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions          = { RuleName = each.value.name }
}

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${local.name}-rds-cpu"
  alarm_description   = "RDS CPU is above 80 % for 15 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = local.alarm_actions
  dimensions          = { DBInstanceIdentifier = aws_db_instance.this.identifier }
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${local.name}-rds-free-storage"
  alarm_description   = "RDS free storage is below 5 GB."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5 * 1024 * 1024 * 1024
  comparison_operator = "LessThanThreshold"
  alarm_actions       = local.alarm_actions
  dimensions          = { DBInstanceIdentifier = aws_db_instance.this.identifier }
}

resource "aws_cloudwatch_metric_alarm" "rds_memory" {
  alarm_name          = "${local.name}-rds-freeable-memory"
  alarm_description   = "RDS freeable memory is below 100 MB."
  namespace           = "AWS/RDS"
  metric_name         = "FreeableMemory"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 100 * 1024 * 1024
  comparison_operator = "LessThanThreshold"
  alarm_actions       = local.alarm_actions
  dimensions          = { DBInstanceIdentifier = aws_db_instance.this.identifier }
}

resource "aws_cloudwatch_metric_alarm" "rds_connections" {
  alarm_name          = "${local.name}-rds-connections"
  alarm_description   = "RDS has more than 300 connections."
  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 300
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = local.alarm_actions
  dimensions          = { DBInstanceIdentifier = aws_db_instance.this.identifier }
}

resource "aws_cloudwatch_metric_alarm" "valkey_memory" {
  alarm_name          = "${local.name}-valkey-memory"
  alarm_description   = "Valkey memory use is above 80 %."
  namespace           = "AWS/ElastiCache"
  metric_name         = "DatabaseMemoryUsagePercentage"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = local.alarm_actions
  dimensions          = { ReplicationGroupId = aws_elasticache_replication_group.this.id }
}

resource "aws_cloudwatch_metric_alarm" "valkey_cpu" {
  alarm_name          = "${local.name}-valkey-cpu"
  alarm_description   = "Valkey engine CPU is above 80 %."
  namespace           = "AWS/ElastiCache"
  metric_name         = "EngineCPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = local.alarm_actions
  dimensions          = { ReplicationGroupId = aws_elasticache_replication_group.this.id }
}

# Error lines in service logs (LOG_FORMAT=json writes "level": "ERROR").
resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each = toset(["backend", "worker"])

  name           = "${local.name}-${each.key}-errors"
  log_group_name = aws_cloudwatch_log_group.service[each.key].name
  pattern        = "{ $.level = \"ERROR\" || $.level = \"CRITICAL\" }"

  metric_transformation {
    name          = "${each.key}-error-lines"
    namespace     = "Skynet/${var.environment}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "log_errors" {
  for_each = aws_cloudwatch_log_metric_filter.errors

  alarm_name          = "${local.name}-${each.key}-error-logs"
  alarm_description   = "${each.key} wrote more than 50 error lines in 5 minutes."
  namespace           = "Skynet/${var.environment}"
  metric_name         = each.value.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 50
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# ---------------------------------------------------------------- backups
# RDS sends no "backup failed" event, so the check is freshness: every
# automated backup ends with RDS-EVENT-0002, and no such event for 30 hours
# (backups run daily at 08:00 UTC) raises the alarm.
resource "aws_db_event_subscription" "rds_failure" {
  name             = "${local.name}-rds-failure"
  sns_topic        = aws_sns_topic.alarms.arn
  source_type      = "db-instance"
  source_ids       = [aws_db_instance.this.identifier]
  event_categories = ["failure"]
}

resource "aws_cloudwatch_event_rule" "rds_backup_done" {
  name        = "${local.name}-rds-backup-done"
  description = "Automated RDS backup finished."

  event_pattern = jsonencode({
    source        = ["aws.rds"]
    "detail-type" = ["RDS DB Instance Event"]
    detail = {
      EventID          = ["RDS-EVENT-0002"]
      SourceIdentifier = [aws_db_instance.this.identifier]
    }
  })
}

# The log keeps a record of each backup. The alarm reads the rule's own
# MatchedEvents metric, so it does not depend on log delivery.
resource "aws_cloudwatch_log_group" "rds_backups" {
  name              = "/aws/events/${local.name}-rds-backups"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_resource_policy" "rds_backups" {
  policy_name = "${local.name}-rds-backups"

  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.rds_backups.arn}:*"
      Condition = {
        ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.rds_backup_done.arn }
      }
    }]
  })
}

resource "aws_cloudwatch_event_target" "rds_backup_done" {
  rule = aws_cloudwatch_event_rule.rds_backup_done.name
  arn  = aws_cloudwatch_log_group.rds_backups.arn
}

resource "aws_cloudwatch_metric_alarm" "rds_backup_missing" {
  alarm_name          = "${local.name}-rds-backup-missing"
  alarm_description   = "No automated RDS backup finished in 30 hours. Check RDS events and the automated backups of ${aws_db_instance.this.identifier}."
  namespace           = "AWS/Events"
  metric_name         = "MatchedEvents"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 30
  datapoints_to_alarm = 30
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions          = { RuleName = aws_cloudwatch_event_rule.rds_backup_done.name }

  # A new rule has no data yet. Without a warm-up the alarm fires right after
  # create; with it, evaluation starts at the first backup (or after 2 days).
  warm_up_configuration {
    warm_up_period_duration_in_minutes = 2880
  }
}
