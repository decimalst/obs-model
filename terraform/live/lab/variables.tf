variable "account_id" {
  description = "Explicit lab account ID; supplied in the private run variables, never inferred from an administrator ARN."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the exact 12-digit authorized lab account ID."
  }
}
variable "name" {
  description = "Unique olly-lab- resource prefix, at most 18 characters; never reuse a production deployment name or state key."
  type        = string
  validation {
    condition     = can(regex("^olly-lab-[a-z0-9]([a-z0-9-]{0,8}[a-z0-9])?$", var.name))
    error_message = "Use a unique olly-lab- name with 1-10 lowercase/digit suffix characters (18 total maximum)."
  }
}
variable "region" {
  type    = string
  default = "us-east-2"
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]$", var.region))
    error_message = "Use an explicitly selected commercial AWS region."
  }
}
variable "availability_zones" {
  type = list(string)
  validation {
    condition = (
      length(var.availability_zones) == 3 &&
      length(toset(var.availability_zones)) == 3 &&
      alltrue([for zone in var.availability_zones : can(regex("^${var.region}[a-z]$", zone))])
    )
    error_message = "Supply three distinct AZ names in the selected lab region."
  }
}
variable "admin_principal_arn" {
  description = "Existing administrator/SSO IAM role in the selected account; a role ARN, never an STS session ARN."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9+=,.@_/-]+$", var.admin_principal_arn))
    error_message = "Supply the existing administrator IAM role ARN in the explicitly selected lab account."
  }
}
variable "tags" {
  description = "Required lab ownership and expiry metadata. ExpiresAt is a review/cleanup signal, not an automatic deletion mechanism."
  type        = map(string)
  validation {
    condition = (
      alltrue([for key in ["Owner", "Environment", "CostCenter", "LabRun", "ExpiresAt"] : try(length(trimspace(var.tags[key])) > 0, false)]) &&
      try(var.tags["Environment"] == "lab", false) &&
      can(regex("^[a-z0-9][a-z0-9-]{2,63}$", var.tags["LabRun"])) &&
      can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.tags["ExpiresAt"])) &&
      can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.tags["ExpiresAt"]))
    )
    error_message = "Set Owner, Environment=lab, CostCenter, LabRun and a valid RFC3339 ExpiresAt timestamp."
  }
}
variable "quota_redis_auth_token" {
  description = "Generated lab quota-store password; use a protected environment input and encrypted state. Never put it in tracked tfvars or logs."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[A-Za-z0-9]{32,128}$", var.quota_redis_auth_token))
    error_message = "Use a generated 32-128 character alphanumeric quota-store password."
  }
}
variable "runner_ami_id" {
  description = "Exact reviewed regional x86_64 Amazon Linux AMI with SSM Agent installed; resolve and verify it outside this root. No floating AMI lookup."
  type        = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.runner_ami_id))
    error_message = "Pin an exact reviewed 17-hex-digit regional AMI ID."
  }
}
variable "enable_profiling" {
  description = "Explicit optional profile-storage provisioning; privileged profiling deployment requires its separate qualification."
  type        = bool
  default     = false
}
