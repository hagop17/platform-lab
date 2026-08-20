# boundary.tf
# A permissions boundary is a CEILING, not a grant. Effective permissions
# are the intersection of the identity policy and this document; a role
# holding only a boundary can do nothing at all.
#
# Written by hand from intent rather than derived from observed behaviour:
# it describes what should never be possible, and there is nothing to
# observe. The realistic failure it defends against is not an attacker but
# widening the identity policy under time pressure on a billing cluster.

data "aws_iam_policy_document" "deployer_boundary" {

  # Deliberately broad. The boundary must be a SUPERSET of the identity
  # policy; if it is tighter, calls fail with AccessDenied that looks like
  # an identity-policy bug.
  statement {
    sid    = "CeilingAllow"
    effect = "Allow"
    actions = [
      "eks:*",
      "ec2:*",
      "autoscaling:*",
      "ecr:*",
      "logs:*",
      "cloudwatch:*",
      "sts:GetCallerIdentity",
      "iam:GetRole",
      "iam:ListRole*",
      "iam:ListAttachedRolePolicies",
      "iam:CreateServiceLinkedRole",
    ]
    resources = ["*"]
  }

  # PassRole is deliberately NOT in CeilingAllow above. It is the one action
  # here that converts "can call EC2" into "can become another identity", so
  # leaving it at resources = ["*"] would make the ceiling useless against
  # exactly the failure this file exists to prevent: a widened identity
  # policy. With ec2:* already in the ceiling, an identity policy loosened to
  # iam:PassRole on "*" would let the deployer launch an instance carrying any
  # role in the account — DenyPrivilegeEscalation would not stop it, because
  # no role is being created, only passed.
  #
  # Scoped identically to the identity policy's PassClusterAndNodeRoles
  # statement, so this narrows nothing that works today.
  statement {
    sid       = "CeilingPassRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.eks_cluster.arn, aws_iam_role.eks_node.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["eks.amazonaws.com", "ec2.amazonaws.com"]
    }
  }

  statement {
    sid     = "CeilingStateBackend"
    effect  = "Allow"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::${var.tfstate_bucket_name}",
      "arn:aws:s3:::${var.tfstate_bucket_name}/*",
    ]
  }

  statement {
    sid       = "CeilingStateLock"
    effect    = "Allow"
    actions   = ["dynamodb:*"]
    resources = ["arn:aws:dynamodb:us-west-2:${var.account_id}:table/${var.tflock_table_name}"]
  }

  # Denies always win, whatever any identity policy allows.
  statement {
    sid    = "DenyPrivilegeEscalation"
    effect = "Deny"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:UpdateAssumeRolePolicy",
      "iam:CreateUser",
      "iam:CreatePolicy",
      "iam:CreatePolicyVersion",
    ]
    resources = ["*"]
  }

  # Without this the role could remove its own ceiling and the boundary
  # would be decorative.
  statement {
    sid    = "DenyBoundaryTampering"
    effect = "Deny"
    actions = [
      "iam:PutRolePermissionsBoundary",
      "iam:DeleteRolePermissionsBoundary",
    ]
    resources = ["*"]
  }

  # CloudTrail is the evidence used to close AccessDenied gaps reactively.
  statement {
    sid    = "DenyAuditTampering"
    effect = "Deny"
    actions = [
      "cloudtrail:StopLogging",
      "cloudtrail:DeleteTrail",
      "cloudtrail:PutEventSelectors",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "DenyStateBucketDeletion"
    effect    = "Deny"
    actions   = ["s3:DeleteBucket"]
    resources = ["*"]
  }

  statement {
    sid       = "DenyAccountLevel"
    effect    = "Deny"
    actions   = ["organizations:*", "account:*"]
    resources = ["*"]
  }

  # Region lock. The not_actions carve-out is MANDATORY: global services
  # report no region or us-east-1, so a naive condition would break every
  # IAM and STS call — including the AssumeRole that starts a session.
  statement {
    sid    = "DenyOtherRegions"
    effect = "Deny"
    not_actions = [
      "iam:*",
      "sts:*",
      "route53:*",
      "cloudfront:*",
      "organizations:*",
    ]
    resources = ["*"]

    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = ["us-west-2"]
    }
  }
}

resource "aws_iam_policy" "deployer_boundary" {
  name        = "platform-lab-deployer-boundary"
  description = "Maximum permissions platform-lab-deployer can ever hold"
  policy      = data.aws_iam_policy_document.deployer_boundary.json

  tags = {
    project = "platform-lab"
  }
}
