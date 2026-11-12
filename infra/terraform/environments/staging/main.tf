# Staging. Sizing comes from terraform.tfvars (lean) or full.tfvars (full).

module "env" {
  source = "../../modules/environment"

  environment = "staging"
  region      = var.region
  image_tag   = var.image_tag

  services_enabled = var.services_enabled

  # ALB and RDS subnet groups need subnets in two AZs, so the VPC always
  # spans two. The single-AZ choices are NAT, RDS and Valkey (tfvars).
  vpc_cidr    = "10.40.0.0/16"
  az_count    = 2
  egress_mode = var.egress_mode

  frontend_domains = ["staging.skynetml.com"]
  api_domain       = "api-staging.skynetml.com"
  mcp_domain       = "mcp.staging.skynetml.com"
  enable_https     = var.enable_https

  # mcp.staging.skynetml.com uses the *.staging.skynetml.com certificate.
  # Cloudflare: *.staging CNAME to the ALB.
  wildcard_domain = "staging.skynetml.com"

  db_instance_class        = var.db_instance_class
  db_multi_az              = var.db_multi_az
  db_backup_retention_days = 3
  db_deletion_protection   = false

  cache_node_count = var.cache_node_count

  ecr_repository_urls = local.ecr_repository_urls

  services = var.services

  # Staging-only name from the Railway export.
  app_secret_keys_extra    = ["PREVIEW_GRANT_CENTS"]
  worker_secret_keys_extra = ["PREVIEW_GRANT_CENTS"]

  log_retention_days = 14
  alert_email        = var.alert_email
}
