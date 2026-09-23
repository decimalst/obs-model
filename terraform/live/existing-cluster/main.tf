terraform {
  required_version = ">= 1.10, < 2.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
provider "aws" {
  region = var.region
  dynamic "assume_role" {
    for_each = var.terraform_role_arn == null ? [] : [var.terraform_role_arn]
    content { role_arn = assume_role.value }
  }
}
data "aws_eks_cluster" "existing" {
  name = var.cluster_name
  lifecycle {
    postcondition {
      condition     = self.status == "ACTIVE"
      error_message = "The existing EKS cluster must be ACTIVE."
    }
  }
}
data "aws_vpc" "existing" { id = data.aws_eks_cluster.existing.vpc_config[0].vpc_id }
data "aws_subnet" "selected" {
  for_each = toset(var.private_subnet_ids)
  id       = each.value
  lifecycle {
    postcondition {
      condition     = self.vpc_id == data.aws_vpc.existing.id && !self.map_public_ip_on_launch
      error_message = "Data-service subnets must belong to the cluster VPC and must not assign public IPs. Verify their route tables are private too."
    }
  }
}
data "aws_security_group" "client" {
  for_each = var.additional_client_security_group_ids
  id       = each.value
  lifecycle {
    postcondition {
      condition     = self.vpc_id == data.aws_vpc.existing.id
      error_message = "Client node/pod security groups must belong to the cluster VPC."
    }
  }
}
module "platform_services" {
  enable_profiling                     = var.enable_profiling
  source                               = "../../modules/platform-services"
  name                                 = var.name
  region                               = var.region
  cluster_name                         = data.aws_eks_cluster.existing.name
  cluster_arn                          = data.aws_eks_cluster.existing.arn
  vpc_id                               = data.aws_vpc.existing.id
  vpc_cidr                             = data.aws_vpc.existing.cidr_block
  private_subnet_ids                   = var.private_subnet_ids
  availability_zones                   = [for id in var.private_subnet_ids : data.aws_subnet.selected[id].availability_zone]
  cluster_security_group_id            = data.aws_eks_cluster.existing.vpc_config[0].cluster_security_group_id
  additional_client_security_group_ids = toset([for sg in data.aws_security_group.client : sg.id])
  existing_role_arns                   = var.existing_role_arns
  manage_pod_identity_associations     = var.manage_pod_identity_associations
  quota_redis_auth_token               = var.quota_redis_auth_token
  tags                                 = var.tags
}
output "platform" {
  value = merge(module.platform_services.platform, {
    operators = { cluster-autoscaler = false, metrics-server = false }
  })
}
output "required_role_policies" { value = module.platform_services.required_role_policies }
