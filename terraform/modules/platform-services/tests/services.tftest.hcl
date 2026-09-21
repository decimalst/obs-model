mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
}
variables {
  name                      = "olly-test"
  region                    = "us-east-1"
  cluster_name              = "existing"
  cluster_arn               = "arn:aws:eks:us-east-1:123456789012:cluster/existing"
  vpc_id                    = "vpc-12345678"
  vpc_cidr                  = "10.0.0.0/16"
  cluster_security_group_id = "sg-12345678"
  private_subnet_ids        = ["subnet-11111111", "subnet-22222222", "subnet-33333333"]
  availability_zones        = ["us-east-1b", "us-east-1d", "us-east-1f"]
  quota_redis_auth_token    = "testpasswordtestpasswordtestpassword"
  tags                      = { Owner = "test", Environment = "test", CostCenter = "test" }
}
run "protected_regional_services" {
  command = plan
  assert {
    condition     = alltrue([for b in aws_s3_bucket.telemetry : !b.force_destroy]) && aws_db_instance.grafana.multi_az && aws_db_instance.grafana.storage_encrypted && aws_db_instance.grafana.deletion_protection
    error_message = "Data must be durable, encrypted, protected, and available across AZs."
  }
  assert {
    condition     = aws_elasticache_replication_group.quota.num_cache_clusters == 3 && aws_elasticache_replication_group.quota.automatic_failover_enabled && aws_elasticache_replication_group.quota.transit_encryption_enabled
    error_message = "Shared quotas need encrypted Multi-AZ failover."
  }
  assert {
    condition     = length(aws_iam_role.storage) == 3 && length(aws_eks_pod_identity_association.storage) == 3 && output.platform.availability_zones == var.availability_zones
    error_message = "Managed mode creates all roles and preserves actual AZ coordinates."
  }
}
run "reuse_roles_without_policy_mutation" {
  command = plan
  variables {
    existing_role_arns = {
      mimir = "arn:aws:iam::123456789012:role/existing-mimir"
      tempo = "arn:aws:iam::123456789012:role/existing-tempo"
      flink = "arn:aws:iam::123456789012:role/existing-flink"
    }
  }
  assert {
    condition     = length(aws_iam_role.storage) == 0 && length(aws_iam_role_policy.storage) == 0 && length(aws_eks_pod_identity_association.storage) == 3
    error_message = "Reused roles must not be created or receive policy mutations."
  }
  assert {
    condition     = aws_eks_pod_identity_association.storage["mimir"].role_arn == var.existing_role_arns.mimir
    error_message = "The supplied role must be used for its service account."
  }
}
run "externally_owned_associations" {
  command = plan
  variables {
    manage_pod_identity_associations = false
    existing_role_arns = {
      mimir = "arn:aws:iam::123456789012:role/existing-mimir"
      tempo = "arn:aws:iam::123456789012:role/existing-tempo"
      flink = "arn:aws:iam::123456789012:role/existing-flink"
    }
  }
  assert {
    condition     = length(aws_eks_pod_identity_association.storage) == 0
    error_message = "Externally owned associations must not be managed."
  }
}
run "reject_disconnected_generated_roles" {
  command = plan
  variables { manage_pod_identity_associations = false }
  expect_failures = [var.existing_role_arns]
}
run "reject_duplicate_subnets" {
  command = plan
  variables { private_subnet_ids = ["subnet-11111111", "subnet-11111111", "subnet-33333333"] }
  expect_failures = [var.private_subnet_ids]
}
run "reject_foreign_az" {
  command = plan
  variables { availability_zones = ["us-east-1a", "us-east-1b", "us-west-2c"] }
  expect_failures = [var.availability_zones]
}
run "reject_unknown_role_component" {
  command = plan
  variables { existing_role_arns = { typo = "arn:aws:iam::123456789012:role/test" } }
  expect_failures = [var.existing_role_arns]
}
run "reject_cross_account_direct_role" {
  command = plan
  variables { existing_role_arns = { mimir = "arn:aws:iam::999999999999:role/test" } }
  expect_failures = [aws_eks_pod_identity_association.storage]
}
