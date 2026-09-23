output "platform" {
  value = {
    profiling             = { enabled = var.enable_profiling }
    cluster_name          = var.cluster_name
    cluster_arn           = var.cluster_arn
    region                = var.region
    availability_zones    = var.availability_zones
    buckets               = { for k, v in aws_s3_bucket.telemetry : k => v.id }
    grafana_db_host       = aws_db_instance.grafana.address
    grafana_db_secret_arn = aws_db_instance.grafana.master_user_secret[0].secret_arn
    quota_redis_host      = aws_elasticache_replication_group.quota.primary_endpoint_address
    quota_secret_arn      = aws_secretsmanager_secret.quota.arn
    vpc_cidr              = var.vpc_cidr
    role_arns             = local.role_arns
  }
}
output "required_role_policies" {
  description = "Review these least-privilege policy documents when reusing externally managed roles; Terraform does not attach policies to those roles."
  value       = { for k, v in data.aws_iam_policy_document.storage : k => v.json }
}
