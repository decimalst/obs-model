variable "name" { type = string }
variable "region" { type = string }
variable "cluster_name" { type = string }
variable "cluster_arn" { type = string }
variable "vpc_id" { type = string }
variable "vpc_cidr" { type = string }
variable "cluster_security_group_id" { type = string }
variable "private_subnet_ids" {
  type = list(string)
  validation {
    condition     = length(var.private_subnet_ids) == 3 && length(toset(var.private_subnet_ids)) == 3
    error_message = "Supply three distinct private subnets, one per selected AZ."
  }
}
variable "availability_zones" {
  type = list(string)
  validation {
    condition     = length(var.availability_zones) == 3 && length(toset(var.availability_zones)) == 3 && alltrue([for az in var.availability_zones : can(regex("^${var.region}[a-z]$", az))])
    error_message = "Three distinct standard AZs in this region are required."
  }
}
variable "additional_client_security_group_ids" {
  description = "Additional node/pod security groups permitted to reach the NEW data-services security group. Existing security groups are never modified."
  type        = set(string)
  default     = []
  validation {
    condition     = !contains(var.additional_client_security_group_ids, var.cluster_security_group_id)
    error_message = "The cluster security group is already allowed; provide only additional groups."
  }
}
variable "existing_role_arns" {
  description = "Optional existing Pod Identity roles for mimir, tempo, flink, and optional pyroscope. Their trust and policies remain externally managed."
  type        = map(string)
  default     = {}
  validation {
    condition     = var.manage_pod_identity_associations || alltrue([for key in concat(["mimir", "tempo", "flink"], var.enable_profiling ? ["pyroscope"] : []) : contains(keys(var.existing_role_arns), key)])
    error_message = "Externally managed associations require all three existing workload role ARNs."
  }
  validation {
    condition     = alltrue([for k, v in var.existing_role_arns : contains(["mimir", "tempo", "flink", "pyroscope"], k) && can(regex("^arn:aws:iam::[0-9]{12}:role/.+", v))])
    error_message = "Only mimir, tempo, flink, and optional pyroscope IAM role ARNs are accepted."
  }
}
variable "manage_pod_identity_associations" {
  description = "False only when the cluster owner already manages the exact service-account associations."
  type        = bool
  default     = true
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
variable "tags" {
  type = map(string)
  validation {
    condition     = alltrue([for k in ["Owner", "Environment", "CostCenter"] : try(length(trimspace(var.tags[k])) > 0, false)])
    error_message = "Owner, Environment, and CostCenter tags are required."
  }
}

variable "enable_profiling" {
  description = "Create regional profile storage and its Pod Identity association. Disabling after use requires a retention/migration review."
  type        = bool
  default     = false
}
