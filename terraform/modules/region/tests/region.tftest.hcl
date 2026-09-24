mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_eks_addon_version" {
    defaults = { version = "v1.20.0-eksbuild.1" }
  }
}
variables {
  name                   = "olly-test"
  region                 = "us-east-1"
  vpc_cidr               = "10.40.0.0/16"
  availability_zones     = ["us-east-1a", "us-east-1b", "us-east-1c"]
  admin_principal_arn    = "arn:aws:iam::123456789012:role/test"
  quota_redis_auth_token = "testpasswordtestpasswordtestpassword"
  tags                   = { Owner = "test", Environment = "test", CostCenter = "test" }
}
run "regional_ha_and_cost_contracts" {
  command = plan
  assert {
    condition     = length(aws_nat_gateway.this) == 3 && length(aws_eks_node_group.this) == 3
    error_message = "Every AZ needs local NAT and an independent node group."
  }
  assert {
    condition     = !aws_eks_cluster.this.vpc_config[0].endpoint_public_access && aws_eks_cluster.this.vpc_config[0].endpoint_private_access
    error_message = "EKS must remain private."
  }
  assert {
    condition     = aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway"
    error_message = "S3 traffic must bypass NAT."
  }
  assert {
    condition     = aws_launch_template.node.metadata_options[0].http_tokens == "required"
    error_message = "IMDSv2 must be required."
  }
}
run "iam_infrastructure_roles_and_administrator_grant" {
  command = plan
  override_data {
    target = data.aws_iam_policy_document.eks_trust
    values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[],\"Id\":\"mock-eks-trust\"}" }
  }
  override_data {
    target = data.aws_iam_policy_document.ec2_trust
    values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[],\"Id\":\"mock-node-trust\"}" }
  }
  override_data {
    target = data.aws_iam_policy_document.pod_trust
    values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[],\"Id\":\"mock-pod-trust\"}" }
  }
  assert {
    condition = (
      aws_iam_role.cluster.name_prefix == "olly-test-eks-" &&
      aws_iam_role.node.name_prefix == "olly-test-node-" &&
      aws_iam_role.autoscaler.name_prefix == "olly-test-autoscaler-" &&
      toset(keys(aws_iam_role.addon)) == toset(["ebs"]) &&
      aws_iam_role.cluster.assume_role_policy == data.aws_iam_policy_document.eks_trust.json &&
      aws_iam_role.node.assume_role_policy == data.aws_iam_policy_document.ec2_trust.json &&
      aws_iam_role.addon["ebs"].assume_role_policy == data.aws_iam_policy_document.pod_trust.json &&
      aws_iam_role.autoscaler.assume_role_policy == data.aws_iam_policy_document.pod_trust.json
    )
    error_message = "New EKS creates control-plane, node, EBS and autoscaler roles; storage roles are managed by the services module."
  }
  assert {
    condition = (
      one(data.aws_iam_policy_document.eks_trust.statement).actions == toset(["sts:AssumeRole"]) &&
      one(one(data.aws_iam_policy_document.eks_trust.statement).principals).identifiers == toset(["eks.amazonaws.com"]) &&
      one(data.aws_iam_policy_document.ec2_trust.statement).actions == toset(["sts:AssumeRole"]) &&
      one(one(data.aws_iam_policy_document.ec2_trust.statement).principals).identifiers == toset(["ec2.amazonaws.com"])
    )
    error_message = "Control-plane and node roles must trust their distinct AWS service principals."
  }
  assert {
    condition = (
      one(data.aws_iam_policy_document.pod_trust.statement).actions == toset(["sts:AssumeRole", "sts:TagSession"]) &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).principals).identifiers == toset(["pods.eks.amazonaws.com"]) &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).condition).variable == "aws:SourceAccount" &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).condition).test == "StringEquals" &&
      one(one(data.aws_iam_policy_document.pod_trust.statement).condition).values == tolist(["123456789012"])
    )
    error_message = "Infrastructure Pod Identity trusts only the EKS pods service with the current SourceAccount condition."
  }
  assert {
    condition = (
      aws_iam_role_policy_attachment.cluster.policy_arn == "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy" &&
      toset(keys(aws_iam_role_policy_attachment.node)) == toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly", "AmazonEKS_CNI_Policy"]) &&
      aws_iam_role_policy_attachment.addon["ebs"].policy_arn == "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
    )
    error_message = "Documented AWS-managed policies must match the control-plane, shared node/CNI and EBS roles."
  }
  assert {
    condition = (
      aws_eks_pod_identity_association.addon["ebs"].namespace == "kube-system" &&
      aws_eks_pod_identity_association.addon["ebs"].service_account == "ebs-csi-controller-sa" &&
      aws_eks_pod_identity_association.autoscaler.namespace == "observability-system" &&
      aws_eks_pod_identity_association.autoscaler.service_account == "cluster-autoscaler"
    )
    error_message = "Infrastructure associations must bind only the documented namespace/service-account identities."
  }
  assert {
    condition = (
      aws_eks_access_entry.admin.principal_arn == var.admin_principal_arn &&
      aws_eks_access_entry.admin.type == "STANDARD" &&
      aws_eks_access_policy_association.admin.policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy" &&
      aws_eks_access_policy_association.admin.access_scope[0].type == "cluster" &&
      !aws_eks_cluster.this.access_config[0].bootstrap_cluster_creator_admin_permissions
    )
    error_message = "The existing administrator role receives an explicit cluster-wide EKS grant; provisioning does not automatically grant creator access."
  }
  assert {
    condition = (
      data.aws_iam_policy_document.autoscaler.statement[1].actions == toset(["autoscaling:SetDesiredCapacity", "autoscaling:TerminateInstanceInAutoScalingGroup"]) &&
      data.aws_iam_policy_document.autoscaler.statement[1].resources == toset(["*"]) &&
      one(data.aws_iam_policy_document.autoscaler.statement[1].condition).variable == "aws:ResourceTag/k8s.io/cluster-autoscaler/olly-test" &&
      one(data.aws_iam_policy_document.autoscaler.statement[1].condition).test == "StringEquals" &&
      one(data.aws_iam_policy_document.autoscaler.statement[1].condition).values == tolist(["owned"])
    )
    error_message = "Autoscaler writes require the exact cluster ownership tag even though the resource expression is wildcard."
  }
}
run "storage_role_reuse_does_not_reuse_infrastructure_roles" {
  command = plan
  variables {
    existing_role_arns = {
      mimir = "arn:aws:iam::123456789012:role/existing-mimir"
      tempo = "arn:aws:iam::123456789012:role/existing-tempo"
      flink = "arn:aws:iam::123456789012:role/existing-flink"
    }
  }
  assert {
    condition = (
      alltrue([for component, arn in var.existing_role_arns : output.role_arns[component] == arn]) &&
      aws_iam_role.cluster.name_prefix == "olly-test-eks-" &&
      aws_iam_role.node.name_prefix == "olly-test-node-" &&
      aws_iam_role.autoscaler.name_prefix == "olly-test-autoscaler-" &&
      length(aws_iam_role.addon) == 1 &&
      length(aws_eks_pod_identity_association.addon) == 1
    )
    error_message = "Storage role reuse passes the supplied ARNs through but still creates the new EKS infrastructure identities."
  }
}
run "reject_two_zones" {
  command = plan
  variables { availability_zones = ["us-east-1a", "us-east-1b"] }
  expect_failures = [var.availability_zones]
}
run "reject_cross_region_zone" {
  command = plan
  variables { availability_zones = ["us-east-1a", "us-east-1b", "us-west-2c"] }
  expect_failures = [var.availability_zones]
}
run "reject_missing_ownership" {
  command = plan
  variables { tags = { Environment = "test" } }
  expect_failures = [var.tags]
}
run "reject_too_small_node_pool" {
  command = plan
  variables { nodes_per_az = 1 }
  expect_failures = [var.nodes_per_az]
}
