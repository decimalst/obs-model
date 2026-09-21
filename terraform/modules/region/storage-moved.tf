# Preserve every existing managed resource address during module extraction.
moved {
  from = aws_s3_bucket.telemetry
  to   = module.platform_services.aws_s3_bucket.telemetry
}
moved {
  from = aws_s3_bucket_public_access_block.telemetry
  to   = module.platform_services.aws_s3_bucket_public_access_block.telemetry
}
moved {
  from = aws_s3_bucket_server_side_encryption_configuration.telemetry
  to   = module.platform_services.aws_s3_bucket_server_side_encryption_configuration.telemetry
}
moved {
  from = aws_s3_bucket_versioning.telemetry
  to   = module.platform_services.aws_s3_bucket_versioning.telemetry
}
moved {
  from = aws_s3_bucket_lifecycle_configuration.telemetry
  to   = module.platform_services.aws_s3_bucket_lifecycle_configuration.telemetry
}
moved {
  from = aws_s3_bucket_policy.telemetry
  to   = module.platform_services.aws_s3_bucket_policy.telemetry
}
moved {
  from = aws_iam_role.storage
  to   = module.platform_services.aws_iam_role.storage
}
moved {
  from = aws_iam_role_policy.storage
  to   = module.platform_services.aws_iam_role_policy.storage
}
moved {
  from = aws_eks_pod_identity_association.storage
  to   = module.platform_services.aws_eks_pod_identity_association.storage
}
moved {
  from = aws_db_subnet_group.grafana
  to   = module.platform_services.aws_db_subnet_group.grafana
}
moved {
  from = aws_security_group.data
  to   = module.platform_services.aws_security_group.data
}
moved {
  from = aws_vpc_security_group_ingress_rule.postgres
  to   = module.platform_services.aws_vpc_security_group_ingress_rule.postgres
}
moved {
  from = aws_vpc_security_group_ingress_rule.redis
  to   = module.platform_services.aws_vpc_security_group_ingress_rule.redis
}
moved {
  from = aws_db_instance.grafana
  to   = module.platform_services.aws_db_instance.grafana
}
moved {
  from = aws_secretsmanager_secret.quota
  to   = module.platform_services.aws_secretsmanager_secret.quota
}
moved {
  from = aws_secretsmanager_secret_version.quota
  to   = module.platform_services.aws_secretsmanager_secret_version.quota
}
moved {
  from = aws_elasticache_subnet_group.quota
  to   = module.platform_services.aws_elasticache_subnet_group.quota
}
moved {
  from = aws_elasticache_replication_group.quota
  to   = module.platform_services.aws_elasticache_replication_group.quota
}
moved {
  from = aws_elasticache_parameter_group.quota
  to   = module.platform_services.aws_elasticache_parameter_group.quota
}
