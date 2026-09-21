terraform {
  required_version = ">= 1.10, < 2.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
provider "aws" { region = "us-west-2" }
variable "admin_principal_arn" { type = string }
variable "quota_redis_auth_token" {
  type      = string
  sensitive = true
}
variable "tags" { type = map(string) }
module "region" {
  source                 = "../../modules/region"
  name                   = "olly-us-west-2"
  region                 = "us-west-2"
  vpc_cidr               = "10.41.0.0/16"
  availability_zones     = ["us-west-2a", "us-west-2b", "us-west-2c"]
  admin_principal_arn    = var.admin_principal_arn
  tags                   = var.tags
  quota_redis_auth_token = var.quota_redis_auth_token
}
output "platform" { value = module.region }
