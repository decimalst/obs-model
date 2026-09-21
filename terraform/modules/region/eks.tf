data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_iam_policy_document" "eks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "cluster" {
  name_prefix        = "${var.name}-eks-"
  assume_role_policy = data.aws_iam_policy_document.eks_trust.json
  tags               = local.tags
}
resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}
resource "aws_iam_role" "node" {
  name_prefix        = "${var.name}-node-"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
  tags               = local.tags
}
resource "aws_iam_role_policy_attachment" "node" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly", "AmazonEKS_CNI_Policy"])
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}
resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 14
  tags              = local.tags
}
resource "aws_eks_cluster" "this" {
  name                      = var.name
  role_arn                  = aws_iam_role.cluster.arn
  version                   = var.kubernetes_version
  enabled_cluster_log_types = ["api", "audit", "authenticator"]
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  upgrade_policy { support_type = "STANDARD" }
  vpc_config {
    subnet_ids              = [for subnet in aws_subnet.private : subnet.id]
    endpoint_private_access = true
    endpoint_public_access  = false
  }
  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
  tags       = local.tags
}
resource "aws_eks_access_entry" "admin" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = var.admin_principal_arn
  type          = "STANDARD"
  tags          = local.tags
}
resource "aws_eks_access_policy_association" "admin" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.admin.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}
resource "aws_launch_template" "node" {
  name_prefix = "${var.name}-"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 80
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = local.tags
  }
  tag_specifications {
    resource_type = "volume"
    tags          = local.tags
  }
  tags = local.tags
}
resource "aws_eks_node_group" "this" {
  lifecycle { ignore_changes = [scaling_config[0].desired_size] }
  for_each        = local.zones
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "core-${each.key}"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = [aws_subnet.private[each.key].id]
  instance_types  = var.node_instance_types
  capacity_type   = "ON_DEMAND"
  ami_type        = "AL2023_x86_64_STANDARD"
  scaling_config {
    desired_size = var.nodes_per_az
    min_size     = var.nodes_per_az
    max_size     = var.max_nodes_per_az
  }
  update_config { max_unavailable = 1 }
  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }
  labels     = { "observability/platform" = "true" }
  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.cni]
  tags       = local.tags
}
data "aws_iam_policy_document" "pod_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}
resource "aws_iam_role" "addon" {
  for_each           = toset(["ebs"])
  name_prefix        = "${var.name}-${each.key}-"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
  tags               = local.tags
}
resource "aws_iam_role_policy_attachment" "addon" {
  for_each   = { ebs = "service-role/AmazonEBSCSIDriverPolicy" }
  role       = aws_iam_role.addon[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}
resource "aws_eks_pod_identity_association" "addon" {
  for_each        = { ebs = "ebs-csi-controller-sa" }
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "kube-system"
  service_account = each.value
  role_arn        = aws_iam_role.addon[each.key].arn
  tags            = local.tags
}
data "aws_eks_addon_version" "this" {
  for_each           = toset(["vpc-cni", "coredns", "kube-proxy", "aws-ebs-csi-driver", "eks-pod-identity-agent"])
  addon_name         = each.key
  kubernetes_version = var.kubernetes_version
  most_recent        = false
}
resource "aws_eks_addon" "cni" {
  cluster_name         = aws_eks_cluster.this.name
  addon_name           = "vpc-cni"
  addon_version        = data.aws_eks_addon_version.this["vpc-cni"].version
  configuration_values = jsonencode({ enableNetworkPolicy = "true" })
  depends_on           = [aws_eks_pod_identity_association.addon]
  tags                 = local.tags
}
resource "aws_eks_addon" "this" {
  for_each      = toset(["coredns", "kube-proxy", "aws-ebs-csi-driver", "eks-pod-identity-agent"])
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = each.key
  addon_version = data.aws_eks_addon_version.this[each.key].version
  depends_on    = [aws_eks_node_group.this]
  tags          = local.tags
}
