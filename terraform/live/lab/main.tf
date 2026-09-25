terraform {
  required_version = ">= 1.10, < 2.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.66.0" }
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

data "aws_caller_identity" "this" {
  lifecycle {
    postcondition {
      condition     = self.account_id == var.account_id
      error_message = "The lab credentials must resolve to the explicitly selected account."
    }
  }
}
locals {
  tags = merge(var.tags, {
    ManagedBy = "terraform"
    Platform  = "observability"
    Region    = var.region
    Name      = var.name
  })
  # Six initial workers (24 vCPU), at most nine workers (36 vCPU), plus the
  # 2-vCPU runner. A quota approval is a separate prerequisite, not a resource.
  nodes_per_az     = 2
  max_nodes_per_az = 3
}
module "region" {
  source                 = "../../modules/region"
  name                   = var.name
  region                 = var.region
  vpc_cidr               = "10.72.0.0/16"
  availability_zones     = var.availability_zones
  admin_principal_arn    = var.admin_principal_arn
  tags                   = local.tags
  quota_redis_auth_token = var.quota_redis_auth_token
  enable_profiling       = var.enable_profiling
  node_instance_types    = ["m7i.xlarge"]
  nodes_per_az           = local.nodes_per_az
  max_nodes_per_az       = local.max_nodes_per_az
}

# Derive the private VPC and security group from this newly declared cluster.
# Do not select any unrelated default VPC, production subnet, or cluster.
data "aws_eks_cluster" "this" {
  name = module.region.cluster_name
  lifecycle {
    postcondition {
      condition     = self.arn == "arn:aws:eks:${var.region}:${var.account_id}:cluster/${var.name}"
      error_message = "The discovered EKS cluster must match the lab's exact account, region and name."
    }
  }
}
data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_eks_cluster.this.vpc_config[0].vpc_id]
  }
  filter {
    name   = "tag:LabRun"
    values = [var.tags["LabRun"]]
  }
  filter {
    name   = "tag:Name"
    values = ["${var.name}-private-*"]
  }
  lifecycle {
    postcondition {
      condition     = length(self.ids) == 3
      error_message = "Exactly this lab's three tagged private subnets must be found."
    }
  }
}
data "aws_subnet" "runner" {
  id = sort(data.aws_subnets.private.ids)[0]
  lifecycle {
    postcondition {
      condition = (
        self.vpc_id == data.aws_eks_cluster.this.vpc_config[0].vpc_id &&
        !self.map_public_ip_on_launch &&
        contains(var.availability_zones, self.availability_zone)
      )
      error_message = "The selected runner subnet must be private and belong to the lab VPC and selected AZs."
    }
  }
}
