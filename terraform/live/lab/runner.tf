resource "aws_security_group" "runner" {
  name_prefix = "${var.name}-runner-"
  description = "Private SSM lab runner; no inbound rules"
  vpc_id      = data.aws_eks_cluster.this.vpc_config[0].vpc_id
  tags        = merge(local.tags, { Name = "${var.name}-runner" })
}
resource "aws_vpc_security_group_egress_rule" "runner_https" {
  security_group_id = aws_security_group.runner.id
  description       = "HTTPS for SSM, AWS APIs, reviewed downloads and the private EKS API"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  tags              = local.tags
}
resource "aws_vpc_security_group_ingress_rule" "runner_eks" {
  security_group_id            = data.aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  description                  = "This lab runner to this lab EKS API only"
  referenced_security_group_id = aws_security_group.runner.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  tags                         = local.tags
}
data "aws_iam_policy_document" "runner_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "runner" {
  name_prefix        = "${var.name}-runner-"
  description        = "Private lab installer and read-only AWS preflight; no infrastructure provisioning policy"
  assume_role_policy = data.aws_iam_policy_document.runner_trust.json
  tags               = local.tags
}
resource "aws_iam_role_policy_attachment" "runner_ssm" {
  role       = aws_iam_role.runner.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
data "aws_iam_policy_document" "runner" {
  statement {
    sid       = "RegionalVpcPreflight"
    actions   = ["ec2:DescribeVpcs"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }
  statement {
    sid       = "ThisLabClusterPreflight"
    actions   = ["eks:DescribeCluster", "eks:ListPodIdentityAssociations"]
    resources = [module.region.cluster_arn]
  }
  statement {
    sid       = "ThisLabAssociationsPreflight"
    actions   = ["eks:DescribePodIdentityAssociation"]
    resources = ["arn:aws:eks:${var.region}:${var.account_id}:podidentityassociation/${var.name}/*"]
  }
  statement {
    sid       = "ThisLabPlatformSecretSeeding"
    actions   = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
    resources = [module.region.grafana_db_secret_arn, module.region.quota_secret_arn]
  }
  statement {
    sid       = "ThisLabBucketRegionPreflight"
    actions   = ["s3:GetBucketLocation"]
    resources = [for bucket in values(module.region.buckets) : "arn:aws:s3:::${bucket}"]
  }
  statement {
    sid       = "ReadReviewedLabBundle"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/bundle*"]
  }
}
resource "aws_iam_role_policy" "runner" {
  name   = "lab-install-preflight"
  role   = aws_iam_role.runner.id
  policy = data.aws_iam_policy_document.runner.json
}
resource "aws_iam_instance_profile" "runner" {
  name_prefix = "${var.name}-runner-"
  role        = aws_iam_role.runner.name
  tags        = local.tags
}
resource "aws_instance" "runner" {
  ami                         = var.runner_ami_id
  instance_type               = "t3.medium"
  subnet_id                   = data.aws_subnet.runner.id
  associate_public_ip_address = false
  vpc_security_group_ids      = [aws_security_group.runner.id]
  iam_instance_profile        = aws_iam_instance_profile.runner.name
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 30
    delete_on_termination = true
    tags                  = merge(local.tags, { Name = "${var.name}-runner-root" })
  }
  # AMI provenance and SSM installation are reviewed before supplying its ID.
  # Commands/bundles are supplied later through the separately authorized SSM run.
  user_data                   = "#!/bin/bash\nset -euo pipefail\nsystemctl enable --now amazon-ssm-agent\n"
  user_data_replace_on_change = true
  tags                        = merge(local.tags, { Name = "${var.name}-runner" })
  depends_on = [
    aws_iam_role_policy_attachment.runner_ssm,
    aws_iam_role_policy.runner,
    aws_vpc_security_group_egress_rule.runner_https,
  ]
}
resource "aws_eks_access_entry" "runner" {
  cluster_name  = module.region.cluster_name
  principal_arn = aws_iam_role.runner.arn
  type          = "STANDARD"
  tags          = local.tags
}
resource "aws_eks_access_policy_association" "runner" {
  cluster_name  = module.region.cluster_name
  principal_arn = aws_eks_access_entry.runner.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}
