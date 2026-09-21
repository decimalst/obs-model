data "aws_caller_identity" "current" {}
locals {
  tags      = merge(var.tags, { ManagedBy = "terraform", Platform = "observability", Region = var.region })
  role_arns = merge({ for k, v in aws_iam_role.storage : k => v.arn }, var.existing_role_arns)
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
resource "aws_vpc_security_group_ingress_rule" "additional_postgres" {
  for_each                     = var.additional_client_security_group_ids
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
resource "aws_vpc_security_group_ingress_rule" "additional_redis" {
  for_each                     = var.additional_client_security_group_ids
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}
