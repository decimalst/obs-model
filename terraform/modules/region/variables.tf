variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.name))
    error_message = "Use a 3-24 character lowercase name."
  }
}
variable "region" { type = string }
variable "existing_role_arns" {
  description = "Optional externally managed mimir/tempo/flink Pod Identity role ARNs."
  type        = map(string)
  default     = {}
}
variable "vpc_cidr" { type = string }
variable "availability_zones" {
  type = list(string)
  validation {
    condition     = length(var.availability_zones) == 3 && length(toset(var.availability_zones)) == 3 && alltrue([for az in var.availability_zones : startswith(az, var.region)])
    error_message = "Exactly three distinct availability zones in this region are required."
  }
}
variable "kubernetes_version" {
  type    = string
  default = "1.35"
}
variable "admin_principal_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/", var.admin_principal_arn))
    error_message = "Supply a deployment/admin IAM role ARN, not a user or root ARN."
  }
}
variable "tags" {
  type = map(string)
  validation {
    condition     = alltrue([for k in ["Owner", "Environment", "CostCenter"] : contains(keys(var.tags), k) && try(length(trimspace(var.tags[k])) > 0, false)])
    error_message = "Owner, Environment, and CostCenter tags are required."
  }
}
variable "node_instance_types" {
  type    = list(string)
  default = ["m7i.xlarge"]
}
variable "nodes_per_az" {
  type    = number
  default = 2
  validation {
    condition     = var.nodes_per_az >= 2 && floor(var.nodes_per_az) == var.nodes_per_az
    error_message = "At least two on-demand nodes per AZ are required for quorum and upgrade headroom."
  }
}
variable "max_nodes_per_az" {
  type    = number
  default = 6
  validation {
    condition     = var.max_nodes_per_az >= var.nodes_per_az
    error_message = "Maximum nodes must cover the desired nodes per AZ."
  }
}
variable "grafana_db_instance_class" {
  type    = string
  default = "db.t4g.small"
}
variable "quota_redis_auth_token" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[A-Za-z0-9]{32,128}$", var.quota_redis_auth_token))
    error_message = "Use a generated 32-128 character alphanumeric quota store password."
  }
}

variable "enable_profiling" {
  description = "Create regional profile storage and its Pod Identity association. Disabling after use requires a retention/migration review."
  type        = bool
  default     = false
}
