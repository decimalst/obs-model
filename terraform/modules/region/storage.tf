locals {
  bucket_names = toset(["mimir-blocks", "mimir-ruler", "mimir-alertmanager", "tempo", "flink"])
  storage_roles = {
    mimir = ["mimir-blocks", "mimir-ruler", "mimir-alertmanager"]
    tempo = ["tempo"]
    flink = ["flink"]
  }
}
resource "aws_s3_bucket" "telemetry" {
  for_each      = local.bucket_names
  bucket        = "${var.name}-${data.aws_caller_identity.current.account_id}-${var.region}-${each.key}"
  force_destroy = false
  tags          = local.tags
}
resource "aws_s3_bucket_public_access_block" "telemetry" {
  for_each                = local.bucket_names
  bucket                  = aws_s3_bucket.telemetry[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "telemetry" {
  for_each = local.bucket_names
  bucket   = aws_s3_bucket.telemetry[each.key].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_versioning" "telemetry" {
  for_each = local.bucket_names
  bucket   = aws_s3_bucket.telemetry[each.key].id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_lifecycle_configuration" "telemetry" {
  for_each = local.bucket_names
  bucket   = aws_s3_bucket.telemetry[each.key].id
  rule {
    id     = "bound-noncurrent-and-incomplete-storage"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  # Active object retention belongs to backend compactors, not a blind S3 TTL.
}
data "aws_iam_policy_document" "bucket" {
  for_each = local.bucket_names
  statement {
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.telemetry[each.key].arn, "${aws_s3_bucket.telemetry[each.key].arn}/*"]
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
resource "aws_s3_bucket_policy" "telemetry" {
  for_each = local.bucket_names
  bucket   = aws_s3_bucket.telemetry[each.key].id
  policy   = data.aws_iam_policy_document.bucket[each.key].json
}
resource "aws_iam_role" "storage" {
  for_each           = local.storage_roles
  name_prefix        = "${var.name}-${each.key}-"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
  tags               = local.tags
}
data "aws_iam_policy_document" "storage" {
  for_each = local.storage_roles
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [for name in each.value : aws_s3_bucket.telemetry[name].arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = [for name in each.value : "${aws_s3_bucket.telemetry[name].arn}/*"]
  }
}
resource "aws_iam_role_policy" "storage" {
  for_each = local.storage_roles
  role     = aws_iam_role.storage[each.key].id
  policy   = data.aws_iam_policy_document.storage[each.key].json
}
resource "aws_eks_pod_identity_association" "storage" {
  for_each        = local.storage_roles
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "observability"
  service_account = each.key
  role_arn        = aws_iam_role.storage[each.key].arn
  tags            = local.tags
}
resource "aws_db_subnet_group" "grafana" {
  name       = var.name
  subnet_ids = [for subnet in aws_subnet.private : subnet.id]
  tags       = local.tags
}
resource "aws_security_group" "data" {
  name_prefix = "${var.name}-data-"
  vpc_id      = aws_vpc.this.id
  description = "Regional platform data services"
  tags        = local.tags
}
resource "aws_vpc_security_group_ingress_rule" "postgres" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
resource "aws_vpc_security_group_ingress_rule" "redis" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}
resource "aws_db_instance" "grafana" {
  identifier                   = "${var.name}-grafana"
  engine                       = "postgres"
  engine_version               = "17"
  instance_class               = var.grafana_db_instance_class
  allocated_storage            = 20
  max_allocated_storage        = 100
  storage_type                 = "gp3"
  storage_encrypted            = true
  db_name                      = "grafana"
  username                     = "grafana"
  manage_master_user_password  = true
  multi_az                     = true
  publicly_accessible          = false
  db_subnet_group_name         = aws_db_subnet_group.grafana.name
  vpc_security_group_ids       = [aws_security_group.data.id]
  backup_retention_period      = 7
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.name}-grafana-final"
  auto_minor_version_upgrade   = true
  performance_insights_enabled = false
  tags                         = local.tags
}
# Password is sensitive but present in state; use the encrypted remote backend.
resource "aws_secretsmanager_secret" "quota" {
  name_prefix             = "${var.name}-quota-"
  recovery_window_in_days = 7
  tags                    = local.tags
}
resource "aws_secretsmanager_secret_version" "quota" {
  secret_id     = aws_secretsmanager_secret.quota.id
  secret_string = var.quota_redis_auth_token
}
resource "aws_elasticache_subnet_group" "quota" {
  name       = var.name
  subnet_ids = [for subnet in aws_subnet.private : subnet.id]
  tags       = local.tags
}
resource "aws_elasticache_replication_group" "quota" {
  replication_group_id        = "${var.name}-quota"
  description                 = "Shared regional per-service admission budgets"
  parameter_group_name        = aws_elasticache_parameter_group.quota.name
  engine                      = "valkey"
  engine_version              = "8.0"
  node_type                   = "cache.t4g.small"
  num_cache_clusters          = 3
  automatic_failover_enabled  = true
  multi_az_enabled            = true
  preferred_cache_cluster_azs = var.availability_zones
  subnet_group_name           = aws_elasticache_subnet_group.quota.name
  security_group_ids          = [aws_security_group.data.id]
  at_rest_encryption_enabled  = true
  transit_encryption_enabled  = true
  auth_token                  = var.quota_redis_auth_token
  snapshot_retention_limit    = 1
  tags                        = local.tags
}
resource "aws_elasticache_parameter_group" "quota" {
  name   = "${var.name}-quota"
  family = "valkey8"
  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }
  tags = local.tags
}
