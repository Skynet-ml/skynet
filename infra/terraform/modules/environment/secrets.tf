# Secrets Manager layout per environment (one JSON object per secret):
#
#   skynet/<env>/app               backend + worker settings   (values: filled by hand)
#   skynet/<env>/frontend          frontend settings           (values: filled by hand)
#   skynet/<env>/litellm           litellm settings            (values: filled by hand)
#   skynet/<env>/app-generated     REMOTE_DB_URL, REDIS_URL, APP_DB_PASSWORD (values: Terraform)
#   skynet/<env>/litellm-generated LITELLM_DATABASE_URL, REDIS_PASSWORD, LITELLM_DB_PASSWORD (values: Terraform)
#   skynet/<env>/db-admin          PG* master connection values (values: Terraform)
#
# Task definitions reference single JSON keys ("<arn>:<KEY>::"). ECS refuses
# to start a task when a referenced key is missing, so every key listed below
# must exist in the JSON (an empty string is allowed). scripts/secrets.sh
# prints a template and checks for missing keys, by name only.
#
# Names come from the Railway export (SKY-155). Names that Terraform can
# derive (URLs, ports, data store endpoints) are plain environment values in
# services.tf instead.
#
# The generated values live in the Terraform state too; the state bucket is
# encrypted, versioned and private.

locals {
  # Backend names that the worker does not read.
  backend_only_secret_keys = [
    "GITHUB_OAUTH_CLIENT_ID",
    "GITHUB_OAUTH_CLIENT_SECRET",
    "GITHUB_OAUTH_REDIRECT_URI",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "GOOGLE_PICKER_API_KEY",
    "HF_OAUTH_CLIENT_ID",
    "HF_OAUTH_REDIRECT_URI",
    "MICROSOFT_OAUTH_CLIENT_ID",
    "MICROSOFT_OAUTH_CLIENT_SECRET",
    "MICROSOFT_OAUTH_REDIRECT_URI",
    "NOTION_OAUTH_CLIENT_ID",
    "NOTION_OAUTH_CLIENT_SECRET",
    "NOTION_OAUTH_REDIRECT_URI",
    "OPENROUTER_BALANCE_FLOOR_CREDITS",
    "SUPABASE_OAUTH_CLIENT_ID",
    "SUPABASE_OAUTH_CLIENT_SECRET",
    "SUPABASE_OAUTH_REDIRECT_URI",
    "TRANSCRIPTION_API_KEY",
  ]

  # Names that backend and worker both read.
  shared_app_secret_keys = [
    "ALERT_EMAIL",
    "ALERT_ENVIRONMENT",
    "BACKEND_AUTH_SECRET",
    "BITBUCKET_OAUTH_CLIENT_ID",
    "BITBUCKET_OAUTH_CLIENT_SECRET",
    "BITBUCKET_OAUTH_REDIRECT_URI",
    "BYOK_VAULT_KEY",
    "DSPY_SANDBOX_IMAGE",
    "EMBEDDINGS_API_KEY",
    "EMBEDDINGS_BASE_URL",
    "EMBEDDINGS_DIM",
    "EMBEDDINGS_MODEL",
    "GENERALIST_AGENT_MCP_URL",
    "GITLAB_OAUTH_CLIENT_ID",
    "GITLAB_OAUTH_CLIENT_SECRET",
    "GITLAB_OAUTH_REDIRECT_URI",
    "GLOBAL_DAILY_SPEND_CEILING_CREDITS",
    "LITELLM_PROXY_API_KEY",
    "MAX_MONTHLY_ACTIVE_USERS",
    "MAX_TOTAL_USERS",
    "OPENROUTER_API_KEY",
    "OPENROUTER_PROVISIONING_KEY",
    "SEARCH_BACKEND",
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
    "SENTRY_TRACES_SAMPLE_RATE",
    "SMTP_FROM",
    "SMTP_HOST",
    "SMTP_PASSWORD",
    "SMTP_PORT",
    "SMTP_STARTTLS",
    "SMTP_USERNAME",
    "STRIPE_METER_EVENT_NAME",
    "STRIPE_PRICE_FOUNDERS",
    "STRIPE_PRICE_METERED",
    "STRIPE_PRICE_PACK_PLUS",
    "STRIPE_PRICE_PACK_PRO",
    "STRIPE_PRICE_PACK_STARTER",
    "STRIPE_PRICE_PREMIUM",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "VERCEL_PROJECT_ID",
    "VERCEL_SANDBOX_IMAGE",
    "VERCEL_SANDBOX_MAX_LIFETIME_SECONDS",
    "VERCEL_TEAM_ID",
    "VERCEL_TOKEN",
  ]

  worker_only_secret_keys = ["JOB_RUN_START_METHOD"]

  backend_secret_keys = sort(distinct(concat(
    local.shared_app_secret_keys,
    local.backend_only_secret_keys,
    var.app_secret_keys_extra,
  )))

  worker_secret_keys = sort(distinct(concat(
    local.shared_app_secret_keys,
    local.worker_only_secret_keys,
    var.worker_secret_keys_extra,
  )))

  app_secret_keys = sort(distinct(concat(local.backend_secret_keys, local.worker_secret_keys)))

  frontend_secret_keys = [
    "AUTH_GITHUB_ID",
    "AUTH_GITHUB_SECRET",
    "AUTH_GOOGLE_ID",
    "AUTH_GOOGLE_SECRET",
    "AUTH_SECRET",
    "BACKEND_AUTH_SECRET",
    "NEXT_PUBLIC_SENTRY_DSN",
    "NEXT_PUBLIC_SENTRY_ENVIRONMENT",
    "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE",
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
    "SENTRY_TRACES_SAMPLE_RATE",
  ]

  # LITELLM_SEED_KEY must equal the app's LITELLM_PROXY_API_KEY: start.py
  # registers it as a virtual key in the (new) litellm database on boot.
  litellm_secret_keys = [
    "LITELLM_MASTER_KEY",
    "LITELLM_SEED_KEY",
    "OPENROUTER_API_KEY",
  ]

  manual_secrets = {
    app      = local.app_secret_keys
    frontend = local.frontend_secret_keys
    litellm  = local.litellm_secret_keys
  }

  redis_endpoint = aws_elasticache_replication_group.this.primary_endpoint_address
  db_endpoint    = aws_db_instance.this.address
  app_db_user    = "skynet_app"
}

resource "aws_secretsmanager_secret" "manual" {
  for_each = local.manual_secrets

  name        = "skynet/${var.environment}/${each.key}"
  description = "Skynet ${var.environment} ${each.key} settings. JSON object; keys: see scripts/secrets.sh. Values are set by hand."

  recovery_window_in_days = var.environment == "production" ? 30 : 7
}

resource "aws_secretsmanager_secret" "app_generated" {
  name                    = "skynet/${var.environment}/app-generated"
  description             = "Skynet ${var.environment} app connection URLs generated by Terraform."
  recovery_window_in_days = var.environment == "production" ? 30 : 7
}

# The app connects as skynet_app (owner of the skynet database), never as the
# RDS master. scripts/create-db-roles.sh creates the role.
resource "aws_secretsmanager_secret_version" "app_generated" {
  secret_id = aws_secretsmanager_secret.app_generated.id
  secret_string = jsonencode({
    REMOTE_DB_URL = "postgresql://${local.app_db_user}:${random_password.db_app.result}@${local.db_endpoint}:5432/${aws_db_instance.this.db_name}?sslmode=require"
    REDIS_URL     = "rediss://:${random_password.valkey_auth.result}@${local.redis_endpoint}:6379/0"
    # Read only by the dbadmin task (create-db-roles.sh).
    APP_DB_PASSWORD = random_password.db_app.result
  })
}

resource "aws_secretsmanager_secret" "litellm_generated" {
  name                    = "skynet/${var.environment}/litellm-generated"
  description             = "Skynet ${var.environment} litellm connection values generated by Terraform."
  recovery_window_in_days = var.environment == "production" ? 30 : 7
}

resource "aws_secretsmanager_secret_version" "litellm_generated" {
  secret_id = aws_secretsmanager_secret.litellm_generated.id
  secret_string = jsonencode({
    LITELLM_DATABASE_URL = "postgresql://litellm:${random_password.db_litellm.result}@${local.db_endpoint}:5432/litellm?sslmode=require"
    REDIS_PASSWORD       = random_password.valkey_auth.result
    # Read only by the dbadmin task (create-db-roles.sh).
    LITELLM_DB_PASSWORD = random_password.db_litellm.result
  })
}

# Master credentials for restores and admin work (dbadmin task only).
resource "aws_secretsmanager_secret" "db_admin" {
  name                    = "skynet/${var.environment}/db-admin"
  description             = "Skynet ${var.environment} RDS master credentials (restore and admin only)."
  recovery_window_in_days = var.environment == "production" ? 30 : 7
}

resource "aws_secretsmanager_secret_version" "db_admin" {
  secret_id = aws_secretsmanager_secret.db_admin.id
  secret_string = jsonencode({
    PGHOST     = local.db_endpoint
    PGPORT     = "5432"
    PGUSER     = aws_db_instance.this.username
    PGPASSWORD = random_password.db_master.result
    PGDATABASE = aws_db_instance.this.db_name
    PGSSLMODE  = "require"
  })
}
