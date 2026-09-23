terraform {
  required_version = ">= 1.10, < 2.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
provider "aws" { region = "us-east-1" }
variable "admin_principal_arn" { type = string }
variable "quota_redis_auth_token" {
  type      = string
  sensitive = true
}
variable "tags" { type = map(string) }
variable "existing_role_arns" {
  type    = map(string)
  default = {}
}
variable "enable_profiling" {
  type    = bool
  default = false
}
module "region" {
  enable_profiling       = var.enable_profiling
  source                 = "../../modules/region"
  existing_role_arns     = var.existing_role_arns
  name                   = "olly-us-east-1"
  region                 = "us-east-1"
  vpc_cidr               = "10.40.0.0/16"
  availability_zones     = ["us-east-1a", "us-east-1b", "us-east-1c"]
  admin_principal_arn    = var.admin_principal_arn
  tags                   = var.tags
  quota_redis_auth_token = var.quota_redis_auth_token
}
output "platform" { value = module.region }
