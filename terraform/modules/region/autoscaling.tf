resource "aws_iam_role" "autoscaler" {
  name_prefix        = "${var.name}-autoscaler-"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
  tags               = local.tags
}
data "aws_iam_policy_document" "autoscaler" {
  statement {
    actions   = ["autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeAutoScalingInstances", "autoscaling:DescribeLaunchConfigurations", "autoscaling:DescribeScalingActivities", "autoscaling:DescribeTags", "ec2:DescribeInstanceTypes", "ec2:DescribeImages", "ec2:DescribeLaunchTemplateVersions", "ec2:GetInstanceTypesFromInstanceRequirements", "eks:DescribeNodegroup"]
    resources = ["*"]
  }
  statement {
    actions   = ["autoscaling:SetDesiredCapacity", "autoscaling:TerminateInstanceInAutoScalingGroup"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/k8s.io/cluster-autoscaler/${var.name}"
      values   = ["owned"]
    }
  }
}
resource "aws_iam_role_policy" "autoscaler" {
  role   = aws_iam_role.autoscaler.id
  policy = data.aws_iam_policy_document.autoscaler.json
}
resource "aws_eks_pod_identity_association" "autoscaler" {
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "observability-system"
  service_account = "cluster-autoscaler"
  role_arn        = aws_iam_role.autoscaler.arn
  tags            = local.tags
}
resource "aws_autoscaling_group_tag" "discover" {
  for_each               = local.zones
  autoscaling_group_name = aws_eks_node_group.this[each.key].resources[0].autoscaling_groups[0].name
  tag {
    key                 = "k8s.io/cluster-autoscaler/enabled"
    value               = "true"
    propagate_at_launch = false
  }
}
resource "aws_autoscaling_group_tag" "owned" {
  for_each               = local.zones
  autoscaling_group_name = aws_eks_node_group.this[each.key].resources[0].autoscaling_groups[0].name
  tag {
    key                 = "k8s.io/cluster-autoscaler/${var.name}"
    value               = "owned"
    propagate_at_launch = false
  }
}
