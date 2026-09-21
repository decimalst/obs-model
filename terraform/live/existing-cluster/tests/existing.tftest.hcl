mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
  mock_data "aws_eks_cluster" {
    defaults = {
      name       = "existing"
      arn        = "arn:aws:eks:us-east-1:123456789012:cluster/existing"
      status     = "ACTIVE"
      vpc_config = [{ vpc_id = "vpc-12345678", cluster_security_group_id = "sg-12345678" }]
    }
  }
  mock_data "aws_vpc" { defaults = { id = "vpc-12345678", cidr_block = "10.20.0.0/16" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-12345678", map_public_ip_on_launch = false } }
}
variables {
  region                 = "us-east-1"
  cluster_name           = "existing"
  private_subnet_ids     = ["subnet-11111111", "subnet-22222222", "subnet-33333333"]
  quota_redis_auth_token = "testpasswordtestpasswordtestpassword"
  tags                   = { Owner = "test", Environment = "test", CostCenter = "test" }
}
override_data {
  target = data.aws_subnet.selected["subnet-11111111"]
  values = { vpc_id = "vpc-12345678", availability_zone = "us-east-1b", map_public_ip_on_launch = false }
}
override_data {
  target = data.aws_subnet.selected["subnet-22222222"]
  values = { vpc_id = "vpc-12345678", availability_zone = "us-east-1d", map_public_ip_on_launch = false }
}
override_data {
  target = data.aws_subnet.selected["subnet-33333333"]
  values = { vpc_id = "vpc-12345678", availability_zone = "us-east-1f", map_public_ip_on_launch = false }
}
run "derive_existing_cluster_coordinates" {
  command = plan
  assert {
    condition     = output.platform.availability_zones == tolist(["us-east-1b", "us-east-1d", "us-east-1f"]) && output.platform.cluster_arn == data.aws_eks_cluster.existing.arn
    error_message = "Existing cluster identity and actual subnet AZs must reach installer output."
  }
  assert {
    condition     = !output.platform.operators.cluster-autoscaler && !output.platform.operators.metrics-server
    error_message = "Existing cluster capacity and metrics controllers are external by default."
  }
}
run "reject_foreign_vpc_subnet" {
  command = plan
  override_data {
    target = data.aws_subnet.selected["subnet-33333333"]
    values = { vpc_id = "vpc-foreign", availability_zone = "us-east-1f", map_public_ip_on_launch = false }
  }
  expect_failures = [data.aws_subnet.selected]
}
run "reject_public_address_subnet" {
  command = plan
  override_data {
    target = data.aws_subnet.selected["subnet-33333333"]
    values = { vpc_id = "vpc-12345678", availability_zone = "us-east-1f", map_public_ip_on_launch = true }
  }
  expect_failures = [data.aws_subnet.selected]
}
