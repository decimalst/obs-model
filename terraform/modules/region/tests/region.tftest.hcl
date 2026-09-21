mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_eks_addon_version" {
    defaults = { version = "v1.20.0-eksbuild.1" }
  }
}
variables {
  name                   = "olly-test"
  region                 = "us-east-1"
  vpc_cidr               = "10.40.0.0/16"
  availability_zones     = ["us-east-1a", "us-east-1b", "us-east-1c"]
  admin_principal_arn    = "arn:aws:iam::123456789012:role/test"
  quota_redis_auth_token = "testpasswordtestpasswordtestpassword"
  tags                   = { Owner = "test", Environment = "test", CostCenter = "test" }
}
run "regional_ha_and_cost_contracts" {
  command = plan
  assert {
    condition     = length(aws_nat_gateway.this) == 3 && length(aws_eks_node_group.this) == 3
    error_message = "Every AZ needs local NAT and an independent node group."
  }
  assert {
    condition     = !aws_eks_cluster.this.vpc_config[0].endpoint_public_access && aws_eks_cluster.this.vpc_config[0].endpoint_private_access
    error_message = "EKS must remain private."
  }
  assert {
    condition     = aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway"
    error_message = "S3 traffic must bypass NAT."
  }
  assert {
    condition     = alltrue([for b in aws_s3_bucket.telemetry : !b.force_destroy])
    error_message = "Stateful telemetry must not be silently purged."
  }
  assert {
    condition     = aws_db_instance.grafana.multi_az && aws_db_instance.grafana.storage_encrypted && aws_db_instance.grafana.deletion_protection
    error_message = "Grafana sessions require protected Multi-AZ persistent storage."
  }
  assert {
    condition     = aws_elasticache_replication_group.quota.num_cache_clusters == 3 && aws_elasticache_replication_group.quota.automatic_failover_enabled && aws_elasticache_replication_group.quota.transit_encryption_enabled
    error_message = "Shared log quota state needs encrypted multi-AZ failover."
  }
  assert {
    condition     = aws_launch_template.node.metadata_options[0].http_tokens == "required"
    error_message = "IMDSv2 must be required."
  }
}
run "reject_two_zones" {
  command = plan
  variables { availability_zones = ["us-east-1a", "us-east-1b"] }
  expect_failures = [var.availability_zones]
}
run "reject_cross_region_zone" {
  command = plan
  variables { availability_zones = ["us-east-1a", "us-east-1b", "us-west-2c"] }
  expect_failures = [var.availability_zones]
}
run "reject_missing_ownership" {
  command = plan
  variables { tags = { Environment = "test" } }
  expect_failures = [var.tags]
}
run "reject_too_small_node_pool" {
  command = plan
  variables { nodes_per_az = 1 }
  expect_failures = [var.nodes_per_az]
}
