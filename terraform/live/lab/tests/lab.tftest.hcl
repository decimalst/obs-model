mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
  mock_data "aws_eks_addon_version" { defaults = { version = "v1.20.0-eksbuild.1" } }
  mock_data "aws_eks_cluster" {
    defaults = {
      name       = "olly-lab-test"
      arn        = "arn:aws:eks:us-east-2:123456789012:cluster/olly-lab-test"
      status     = "ACTIVE"
      vpc_config = [{ vpc_id = "vpc-12345678", cluster_security_group_id = "sg-12345678" }]
    }
  }
  mock_data "aws_subnets" { defaults = { ids = ["subnet-11111111", "subnet-22222222", "subnet-33333333"] } }
  mock_data "aws_subnet" {
    defaults = { id = "subnet-11111111", vpc_id = "vpc-12345678", map_public_ip_on_launch = false, availability_zone = "us-east-2a" }
  }
}
variables {
  account_id             = "123456789012"
  name                   = "olly-lab-test"
  region                 = "us-east-2"
  availability_zones     = ["us-east-2a", "us-east-2b", "us-east-2c"]
  admin_principal_arn    = "arn:aws:iam::123456789012:role/ExistingAdmin"
  quota_redis_auth_token = "synthetictestpasswordtestpasswordtestpassword"
  runner_ami_id          = "ami-0123456789abcdef0"
  tags                   = { Owner = "test", Environment = "lab", CostCenter = "test", LabRun = "fixture-only", ExpiresAt = "2026-09-25T18:00:00Z" }
}
# The regional module has its own independent mock suite. Root plan tests use
# its reviewed output contract so discovery can run before a real cluster exists.
override_module {
  target = module.region
  outputs = {
    cluster_name          = "olly-lab-test"
    cluster_arn           = "arn:aws:eks:us-east-2:123456789012:cluster/olly-lab-test"
    region                = "us-east-2"
    availability_zones    = ["us-east-2a", "us-east-2b", "us-east-2c"]
    profiling             = { enabled = false }
    grafana_db_secret_arn = "arn:aws:secretsmanager:us-east-2:123456789012:secret:olly-lab-test-db-ABC123"
    quota_secret_arn      = "arn:aws:secretsmanager:us-east-2:123456789012:secret:olly-lab-test-quota-ABC123"
    buckets               = { "mimir-blocks" = "lab-mimir-blocks", "mimir-ruler" = "lab-mimir-ruler", "mimir-alertmanager" = "lab-mimir-alertmanager", tempo = "lab-tempo", flink = "lab-flink" }
  }
}
# Supply concrete discovery results during the mock plan, even though the real
# data sources correctly wait until the newly declared cluster exists.
override_data {
  target          = data.aws_eks_cluster.this
  override_during = plan
  values = {
    name       = "olly-lab-test"
    arn        = "arn:aws:eks:us-east-2:123456789012:cluster/olly-lab-test"
    status     = "ACTIVE"
    vpc_config = [{ vpc_id = "vpc-12345678", cluster_security_group_id = "sg-12345678" }]
  }
}
override_data {
  target          = data.aws_subnets.private
  override_during = plan
  values          = { ids = ["subnet-11111111", "subnet-22222222", "subnet-33333333"] }
}
override_data {
  target          = data.aws_subnet.runner
  override_during = plan
  values          = { id = "subnet-11111111", vpc_id = "vpc-12345678", map_public_ip_on_launch = false, availability_zone = "us-east-2a" }
}
run "private_scoped_lab_plan" {
  command = plan
  assert {
    condition = (
      aws_instance.runner.ami == var.runner_ami_id &&
      aws_instance.runner.instance_type == "t3.medium" &&
      !aws_instance.runner.associate_public_ip_address &&
      aws_instance.runner.metadata_options[0].http_tokens == "required" &&
      aws_instance.runner.metadata_options[0].http_put_response_hop_limit == 1 &&
      aws_instance.runner.root_block_device[0].encrypted &&
      aws_instance.runner.root_block_device[0].volume_size == 30
    )
    error_message = "Runner must retain its pinned AMI, bounded instance size, private IP and encrypted IMDSv2 configuration."
  }
  assert {
    condition = (
      aws_vpc_security_group_egress_rule.runner_https.from_port == 443 &&
      aws_vpc_security_group_egress_rule.runner_https.to_port == 443 &&
      aws_vpc_security_group_ingress_rule.runner_eks.security_group_id == "sg-12345678" &&
      aws_vpc_security_group_ingress_rule.runner_eks.from_port == 443 &&
      aws_vpc_security_group_ingress_rule.runner_eks.to_port == 443 &&
      data.aws_subnet.runner.id == "subnet-11111111"
    )
    error_message = "Runner connectivity must use only HTTPS and this lab's private subnet / EKS security group."
  }
  assert {
    condition = (
      local.nodes_per_az == 2 && local.max_nodes_per_az == 3 &&
      output.platform.cluster_name == var.name && output.platform.region == var.region &&
      !output.platform.profiling.enabled
    )
    error_message = "The bounded lab keeps six initial workers, a nine-worker maximum and the unchanged installer output contract."
  }
  assert {
    condition = (
      aws_eks_access_entry.runner.type == "STANDARD" &&
      aws_eks_access_entry.runner.cluster_name == var.name &&
      aws_eks_access_policy_association.runner.access_scope[0].type == "cluster" &&
      aws_eks_access_policy_association.runner.policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy" &&
      aws_iam_role_policy_attachment.runner_ssm.policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    )
    error_message = "SSM runner management and Kubernetes installation access must remain explicit, separate privileges."
  }
  assert {
    condition = (
      aws_s3_bucket.artifacts.bucket == "olly-lab-test-123456789012-us-east-2-artifacts" &&
      !aws_s3_bucket.artifacts.force_destroy &&
      aws_s3_bucket_public_access_block.artifacts.block_public_acls &&
      aws_s3_bucket_public_access_block.artifacts.block_public_policy &&
      aws_s3_bucket_public_access_block.artifacts.ignore_public_acls &&
      aws_s3_bucket_public_access_block.artifacts.restrict_public_buckets &&
      one(aws_s3_bucket_server_side_encryption_configuration.artifacts.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" &&
      aws_ecr_repository.quota.image_tag_mutability == "IMMUTABLE" &&
      aws_ecr_repository.quota.image_scanning_configuration[0].scan_on_push &&
      !aws_ecr_repository.quota.force_delete
    )
    error_message = "Private artifacts/images must retain encryption, immutable image tags, scanning and deliberate cleanup."
  }
  assert {
    condition = alltrue([
      for tags in [aws_instance.runner.tags, aws_iam_role.runner.tags, aws_security_group.runner.tags, aws_s3_bucket.artifacts.tags, aws_ecr_repository.quota.tags, aws_eks_access_entry.runner.tags] :
      alltrue([for key in ["Owner", "Environment", "CostCenter", "LabRun", "ExpiresAt"] : tags[key] == var.tags[key]])
    ])
    error_message = "Every owned taggable lab resource must retain the run ownership and expiry markers."
  }
  assert {
    condition = (
      contains([for filter in data.aws_subnets.private.filter : filter if filter.name == "vpc-id"][0].values, "vpc-12345678") &&
      contains([for filter in data.aws_subnets.private.filter : filter if filter.name == "tag:LabRun"][0].values, "fixture-only") &&
      contains([for filter in data.aws_subnets.private.filter : filter if filter.name == "tag:Name"][0].values, "olly-lab-test-private-*")
    )
    error_message = "Subnet discovery must constrain VPC, exact lab run and private name prefix together."
  }
}
run "runner_permissions_do_not_provision_aws" {
  command = plan
  assert {
    condition = toset(flatten([for statement in data.aws_iam_policy_document.runner.statement : tolist(statement.actions)])) == toset([
      "ec2:DescribeVpcs", "eks:DescribeCluster", "eks:ListPodIdentityAssociations", "eks:DescribePodIdentityAssociation",
      "secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue", "s3:GetBucketLocation", "s3:GetObject"
    ])
    error_message = "Custom runner policy must remain read-only in AWS; no wildcard actions, PassRole or provisioning permissions."
  }
  assert {
    condition = (
      one(data.aws_iam_policy_document.runner.statement[0].condition).variable == "aws:RequestedRegion" &&
      one(data.aws_iam_policy_document.runner.statement[0].condition).values == tolist([var.region]) &&
      data.aws_iam_policy_document.runner.statement[2].resources == toset(["arn:aws:eks:us-east-2:123456789012:podidentityassociation/olly-lab-test/*"]) &&
      length(data.aws_iam_policy_document.runner.statement[3].resources) == 2 &&
      data.aws_iam_policy_document.runner.statement[5].actions == toset(["s3:GetObject"])
    )
    error_message = "Only regional VPC discovery may use wildcard resources; cluster associations and two lab Secrets stay scoped."
  }
  assert {
    condition = (
      one(data.aws_iam_policy_document.runner_trust.statement).actions == toset(["sts:AssumeRole"]) &&
      one(one(data.aws_iam_policy_document.runner_trust.statement).principals).identifiers == toset(["ec2.amazonaws.com"]) &&
      one(data.aws_iam_policy_document.artifacts.statement).effect == "Deny" &&
      one(one(data.aws_iam_policy_document.artifacts.statement).condition).variable == "aws:SecureTransport"
    )
    error_message = "The runner trusts EC2 only and artifact access must reject plaintext transport."
  }
}
run "reject_wrong_account_credentials" {
  command = plan
  override_data {
    override_during = plan
    target          = data.aws_caller_identity.this
    values          = { account_id = "999999999999" }
  }
  expect_failures = [data.aws_caller_identity.this]
}
run "reject_wrong_cluster_account_or_region" {
  command = plan
  override_data {
    override_during = plan
    target          = data.aws_eks_cluster.this
    values          = { arn = "arn:aws:eks:us-west-2:999999999999:cluster/olly-lab-test" }
  }
  expect_failures = [data.aws_eks_cluster.this]
}
run "reject_public_runner_subnet" {
  command = plan
  override_data {
    override_during = plan
    target          = data.aws_subnet.runner
    values          = { vpc_id = "vpc-12345678", map_public_ip_on_launch = true, availability_zone = "us-east-2a" }
  }
  expect_failures = [data.aws_subnet.runner]
}
run "reject_ambiguous_subnets" {
  command = plan
  override_data {
    override_during = plan
    target          = data.aws_subnets.private
    values          = { ids = ["subnet-11111111", "subnet-22222222"] }
  }
  expect_failures = [data.aws_subnets.private]
}
run "reject_cross_region_zone" {
  command = plan
  variables { availability_zones = ["us-east-2a", "us-east-2b", "us-west-2c"] }
  expect_failures = [var.availability_zones]
}
run "reject_unpinned_ami" {
  command = plan
  variables { runner_ami_id = "latest" }
  expect_failures = [var.runner_ami_id]
}
run "reject_missing_or_malformed_expiry" {
  command = plan
  variables { tags = { Owner = "test", Environment = "lab", CostCenter = "test", LabRun = "fixture-only", ExpiresAt = "tomorrow" } }
  expect_failures = [var.tags]
}
run "reject_production_environment_tag" {
  command = plan
  variables { tags = { Owner = "test", Environment = "production", CostCenter = "test", LabRun = "fixture-only", ExpiresAt = "2026-09-25T18:00:00Z" } }
  expect_failures = [var.tags]
}

run "reject_production_resource_prefix" {
  command = plan
  variables { name = "olly-production" }
  expect_failures = [var.name]
}
run "reject_long_resource_prefix" {
  command = plan
  variables { name = "olly-lab-12345678901" }
  expect_failures = [var.name]
}
run "reject_timezone_offset_expiry" {
  command = plan
  variables { tags = { Owner = "test", Environment = "lab", CostCenter = "test", LabRun = "fixture-only", ExpiresAt = "2026-09-25T18:00:00+00:00" } }
  expect_failures = [var.tags]
}
run "reject_wildcard_lab_run" {
  command = plan
  variables { tags = { Owner = "test", Environment = "lab", CostCenter = "test", LabRun = "*", ExpiresAt = "2026-09-25T18:00:00Z" } }
  expect_failures = [var.tags]
}
run "reject_external_admin_account" {
  command = plan
  variables { admin_principal_arn = "arn:aws:iam::999999999999:role/OtherAdmin" }
  expect_failures = [var.admin_principal_arn]
}
