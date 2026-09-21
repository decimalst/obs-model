output "cluster_name" { value = aws_eks_cluster.this.name }
output "region" { value = var.region }
output "buckets" { value = { for k, v in aws_s3_bucket.telemetry : k => v.id } }
output "grafana_db_host" { value = aws_db_instance.grafana.address }
output "grafana_db_secret_arn" { value = aws_db_instance.grafana.master_user_secret[0].secret_arn }
output "quota_redis_host" { value = aws_elasticache_replication_group.quota.primary_endpoint_address }
output "quota_secret_arn" { value = aws_secretsmanager_secret.quota.arn }
output "vpc_cidr" { value = var.vpc_cidr }
