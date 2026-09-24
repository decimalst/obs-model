mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock-storage-role" }
  }
  mock_resource "aws_db_instance" {
    defaults = { master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock-grafana" }] }
  }
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
run "storage_iam_scopes_and_exact_bindings" {
  # Mock apply resolves computed bucket ARNs for scope assertions. The mock AWS
  # provider performs no AWS API calls and creates no cloud infrastructure.
  command = apply
  variables { enable_profiling = true }
  override_resource {
    target = aws_iam_role.storage["mimir"]
    values = { arn = "arn:aws:iam::123456789012:role/mock-mimir" }
  }
  override_resource {
    target = aws_iam_role.storage["tempo"]
    values = { arn = "arn:aws:iam::123456789012:role/mock-tempo" }
  }
  override_resource {
    target = aws_iam_role.storage["flink"]
    values = { arn = "arn:aws:iam::123456789012:role/mock-flink" }
  }
  override_resource {
    target = aws_iam_role.storage["pyroscope"]
    values = { arn = "arn:aws:iam::123456789012:role/mock-pyroscope" }
  }
  assert {
    condition = (
      toset(keys(local.storage_roles)) == toset(["mimir", "tempo", "flink", "pyroscope"]) &&
      toset(local.storage_roles.mimir) == toset(["mimir-blocks", "mimir-ruler", "mimir-alertmanager"]) &&
      toset(local.storage_roles.tempo) == toset(["tempo"]) &&
      toset(local.storage_roles.flink) == toset(["flink"]) &&
      toset(local.storage_roles.pyroscope) == toset(["pyroscope"])
    )
    error_message = "Storage roles must keep their documented component-to-bucket boundaries."
  }
  assert {
    condition = alltrue([
      for component, buckets in local.storage_roles : (
        length(data.aws_iam_policy_document.storage[component].statement) == 2 &&
        data.aws_iam_policy_document.storage[component].statement[0].actions == toset(["s3:ListBucket", "s3:GetBucketLocation"]) &&
        data.aws_iam_policy_document.storage[component].statement[0].resources == toset([for bucket in buckets : aws_s3_bucket.telemetry[bucket].arn]) &&
        data.aws_iam_policy_document.storage[component].statement[1].actions == toset(["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]) &&
        data.aws_iam_policy_document.storage[component].statement[1].resources == toset([for bucket in buckets : "${aws_s3_bucket.telemetry[bucket].arn}/*"])
      )
    ])
    error_message = "Storage IAM must grant only the listed bucket operations and object operations inside each component's buckets."
  }
  assert {
    condition = alltrue([
      for component, association in aws_eks_pod_identity_association.storage : (
        association.cluster_name == "existing" &&
        association.namespace == "observability" &&
        association.service_account == component &&
        association.role_arn == aws_iam_role.storage[component].arn
      )
    ])
    error_message = "Every enabled component must bind its own role to the exact cluster, namespace and service account."
  }
  assert {
    condition = (
      one(data.aws_iam_policy_document.pod_trust.statement).actions == toset(["sts:AssumeRole", "sts:TagSession"]) &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).principals).identifiers == toset(["pods.eks.amazonaws.com"]) &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).principals).type == "Service" &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).condition).variable == "aws:SourceAccount" &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).condition).test == "StringEquals" &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).condition).values == tolist(["123456789012"])
    )
    error_message = "Storage Pod Identity trust uses the EKS pods service principal and current SourceAccount."
  }
}
run "partial_storage_role_reuse_preserves_policy_ownership" {
  command = plan
  variables {
    existing_role_arns = { tempo = "arn:aws:iam::123456789012:role/existing-tempo" }
  }
  assert {
    condition = (
      toset(keys(aws_iam_role.storage)) == toset(["mimir", "flink"]) &&
      toset(keys(aws_iam_role_policy.storage)) == toset(["mimir", "flink"]) &&
      toset(keys(aws_eks_pod_identity_association.storage)) == toset(["mimir", "tempo", "flink"]) &&
      aws_eks_pod_identity_association.storage["tempo"].role_arn == var.existing_role_arns.tempo &&
      contains(keys(output.required_role_policies), "tempo")
    )
    error_message = "Partial reuse must leave the supplied role and policies external while retaining its exact association and a reviewable required policy document."
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
    condition     = length(aws_eks_pod_identity_association.storage) == 0 && length(aws_iam_role.storage) == 0 && length(aws_iam_role_policy.storage) == 0
    error_message = "Externally owned associations, supplied roles and policies must not be managed."
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
run "profile_storage_is_optional_regional_and_uses_pod_identity" {
  command = plan
  variables { enable_profiling = true }
  assert {
    condition     = length(aws_s3_bucket.telemetry) == 6 && length(aws_iam_role.storage) == 4 && aws_eks_pod_identity_association.storage["pyroscope"].service_account == "pyroscope" && aws_s3_bucket.telemetry["pyroscope"].force_destroy == false && output.platform.profiling.enabled
    error_message = "Profiles need a protected regional bucket and their own Pod Identity association."
  }
}
run "profile_reused_roles_remain_external" {
  command = plan
  variables {
    enable_profiling   = true
    existing_role_arns = { pyroscope = "arn:aws:iam::123456789012:role/existing-profiles" }
  }
  assert {
    condition     = !contains(keys(aws_iam_role.storage), "pyroscope") && !contains(keys(aws_iam_role_policy.storage), "pyroscope") && aws_eks_pod_identity_association.storage["pyroscope"].role_arn == var.existing_role_arns.pyroscope
    error_message = "Do not adopt or mutate an existing profiling role."
  }
}
run "external_profile_association_requires_profile_role" {
  command = plan
  variables {
    enable_profiling                 = true
    manage_pod_identity_associations = false
    existing_role_arns = {
      mimir = "arn:aws:iam::123456789012:role/existing-mimir"
      tempo = "arn:aws:iam::123456789012:role/existing-tempo"
      flink = "arn:aws:iam::123456789012:role/existing-flink"
    }
  }
  expect_failures = [var.existing_role_arns]
}
