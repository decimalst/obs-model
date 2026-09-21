admin_principal_arn = "arn:aws:iam::123456789012:role/observability-deployer"
tags                = { Owner = "platform-team", Environment = "production", CostCenter = "engineering" }
# Supply TF_VAR_quota_redis_auth_token from a secret manager, not this file.
