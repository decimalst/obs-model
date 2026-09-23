module "platform_services" {
  enable_profiling          = var.enable_profiling
  source                    = "../platform-services"
  name                      = var.name
  region                    = var.region
  cluster_name              = aws_eks_cluster.this.name
  cluster_arn               = aws_eks_cluster.this.arn
  vpc_id                    = aws_vpc.this.id
  vpc_cidr                  = var.vpc_cidr
  private_subnet_ids        = [for az in var.availability_zones : aws_subnet.private[az].id]
  availability_zones        = var.availability_zones
  cluster_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  grafana_db_instance_class = var.grafana_db_instance_class
  quota_redis_auth_token    = var.quota_redis_auth_token
  existing_role_arns        = var.existing_role_arns
  tags                      = var.tags
}
