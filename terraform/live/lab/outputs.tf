# Preserve the production regional module's output shape for install.py.
output "platform" { value = module.region }
output "lab_runner" {
  description = "Nonsecret lab runner/artifact coordinates. No credentials, secret contents or generated commands."
  value = {
    instance_id               = aws_instance.runner.id
    role_arn                  = aws_iam_role.runner.arn
    artifact_bucket           = aws_s3_bucket.artifacts.id
    ecr_repository_url        = aws_ecr_repository.quota.repository_url
    cluster_security_group_id = data.aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
    vpc_id                    = data.aws_eks_cluster.this.vpc_config[0].vpc_id
  }
}
