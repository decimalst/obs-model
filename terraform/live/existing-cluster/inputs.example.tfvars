region             = "us-east-1"
cluster_name       = "my-existing-eks"
name               = "olly-existing"
private_subnet_ids = ["subnet-REPLACE1", "subnet-REPLACE2", "subnet-REPLACE3"]
tags               = { Owner = "platform-team", Environment = "production", CostCenter = "observability" }
# Optional; policies/trust stay under the existing role owner's management.
# existing_role_arns = {
#   mimir = "arn:aws:iam::123456789012:role/existing-mimir"
#   tempo = "arn:aws:iam::123456789012:role/existing-tempo"
# }
# terraform_role_arn = "arn:aws:iam::123456789012:role/deployment"
# additional_client_security_group_ids = ["sg-EXISTING-WORKER-OR-POD-SG"]
# manage_pod_identity_associations = false # only if all three associations already exist
# Supply TF_VAR_quota_redis_auth_token from your secret manager, never in this file.
