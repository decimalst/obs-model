variable "region" { type = string }
variable "cluster_name" { type = string }
variable "name" {
  type    = string
  default = "olly-existing"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.name))
    error_message = "Use a unique 3-24 character lowercase platform name."
  }
}
variable "private_subnet_ids" { type = list(string) }
variable "additional_client_security_group_ids" {
  type    = set(string)
  default = []
}
variable "existing_role_arns" {
  description = "Existing storage workload IAM roles keyed by mimir, tempo, flink, and enabled pyroscope. Trust and permission policies remain owned by the role owner; omitted enabled roles are created."
  type        = map(string)
  default     = {}
}
variable "manage_pod_identity_associations" {
  description = "Create exact observability namespace/service-account associations for the selected roles. Set false only when the cluster owner already manages all enabled associations; this requires all corresponding role ARNs."
  type        = bool
  default     = true
}
variable "terraform_role_arn" {
  description = "Optional existing deployment role to assume. AWS_PROFILE and standard credential chains also work. This does not create or grant EKS access."
  type        = string
  default     = null
}
variable "quota_redis_auth_token" {
  type      = string
  sensitive = true
}
variable "tags" { type = map(string) }

variable "enable_profiling" {
  description = "Create regional profile storage and its Pod Identity association. Disabling after use requires a retention/migration review."
  type        = bool
  default     = false
}
