# iam.tf
# Manages the GitHub OIDC provider, the platform-lab-deployer role, its trust
# policy, and its permissions policy as code.
#
# IMPORTANT: Apply this using hagop-admin credentials, NOT platform-lab-deployer.
# This config manages the deployer role's own permissions — if the deployer
# role tried to apply changes to itself, a bad change could lock it out of
# fixing itself. Keep IAM management separate from the infra the role deploys.
#
# The hagop-admin CLI profile uses `aws login` (browser-based), whose
# credential type Terraform's AWS provider can't resolve directly — so the
# provider block here has no hardcoded `profile`. Export the active session
# as env vars instead, then run terraform with no AWS_PROFILE set:
#
#   eval "$(aws configure export-credentials --profile hagop-admin --format env)"
#   terraform init
#   terraform plan
#   terraform apply
#
# These resources already exist (created manually via console/CLI). Import
# them before your first plan/apply so Terraform adopts them instead of
# trying to create duplicates — see import commands at the bottom of this file.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-west-2"
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "github_org" {
  description = "GitHub organization/user"
  type        = string
  default     = "hagop17"
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "platform-lab"
}

variable "tfstate_bucket_name" {
  description = "Exact S3 bucket name (Account Regional namespace suffix included)"
  type        = string
}

variable "tflock_table_name" {
  description = "DynamoDB table name for Terraform state locking"
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name; scopes the deployer's eks:* write permissions"
  type        = string
  default     = "platform-lab"
}

# ---------------------------------------------------------------------------
# GitHub OIDC identity provider (account-wide; only one should exist per account)
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # Thumbprint is validated dynamically by the AWS provider; a placeholder
  # value is accepted here since AWS no longer strictly enforces thumbprint
  # matching for GitHub's well-known OIDC endpoint, but the field is required.
  thumbprint_list = ["ab9d0263244dd0326eb67015705a667e79cfe998"]

}

# ---------------------------------------------------------------------------
# Trust policy — who can assume platform-lab-deployer
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "deployer_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_org}/${var.github_repo}:*"]
    }
  }

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.account_id}:user/hagop-admin"]
    }
  }
}

# ---------------------------------------------------------------------------
# The deployer role itself
# ---------------------------------------------------------------------------

resource "aws_iam_role" "platform_lab_deployer" {
  name               = "platform-lab-deployer"
  description        = "Deploys platform-lab"
  assume_role_policy = data.aws_iam_policy_document.deployer_trust.json

  # A hard ceiling. See boundary.tf.
  permissions_boundary = aws_iam_policy.deployer_boundary.arn

  # 4 hours. Assumed-role credentials default to 1 hour, and a destroy
  # started at minute 55 can fail partway with ExpiredToken — leaving a
  # half-destroyed cluster still billing, the exact failure this design
  # exists to prevent.
  max_session_duration = 14400

  tags = {
    project = "platform-lab"
  }
}

# ---------------------------------------------------------------------------
# Permissions policy — what the role can actually do
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "deployer_permissions" {

  # Terraform's own bookkeeping — the state file and its lock, nothing
  # more. Scoped to the exact bucket and table.
  statement {
    sid     = "TerraformState"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
    resources = [
      "arn:aws:s3:::${var.tfstate_bucket_name}",
      "arn:aws:s3:::${var.tfstate_bucket_name}/*",
    ]
  }

  statement {
    sid       = "TerraformLock"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable"]
    resources = ["arn:aws:dynamodb:us-west-2:${var.account_id}:table/${var.tflock_table_name}"]
  }

  # Reads are separated from writes so the writes can be ARN-scoped.
  # List/Describe cannot be — AWS gives them no resource-level support.
  statement {
    sid       = "EKSRead"
    actions   = ["eks:List*", "eks:Describe*"]
    resources = ["*"]
  }

  # Everything destructive, locked to resources named after THIS cluster.
  # AccessEntry actions are what grant hagop-admin kubectl access — omit
  # them and you get a cluster you cannot talk to.
  statement {
    sid = "EKSWrite"
    actions = [
      "eks:CreateCluster", "eks:DeleteCluster", "eks:UpdateClusterConfig", "eks:UpdateClusterVersion",
      "eks:CreateNodegroup", "eks:DeleteNodegroup", "eks:UpdateNodegroupConfig", "eks:UpdateNodegroupVersion",
      "eks:CreateAccessEntry", "eks:DeleteAccessEntry", "eks:UpdateAccessEntry",
      "eks:AssociateAccessPolicy", "eks:DisassociateAccessPolicy",
      "eks:TagResource", "eks:UntagResource",
    ]
    resources = [
      "arn:aws:eks:us-west-2:${var.account_id}:cluster/${var.cluster_name}",
      "arn:aws:eks:us-west-2:${var.account_id}:nodegroup/${var.cluster_name}/*",
      "arn:aws:eks:us-west-2:${var.account_id}:access-entry/${var.cluster_name}/*",
    ]
  }

  # The weakest statement, and unavoidably so:
  #   - ec2:Describe* has NO resource-level support in AWS at all
  #   - CreateVpc/CreateSubnet have no pre-existing resource to name
  # Containment comes from the permissions boundary's region lock, not
  # from here. RunInstances/TerminateInstances are deliberately ABSENT —
  # managed node groups launch instances under EKS's own service-linked
  # role, not under this one.
  statement {
    sid = "EC2Networking"
    actions = [
      "ec2:Describe*",
      "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
      "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
      "ec2:CreateInternetGateway", "ec2:DeleteInternetGateway",
      "ec2:AttachInternetGateway", "ec2:DetachInternetGateway",
      "ec2:CreateRouteTable", "ec2:DeleteRouteTable", "ec2:CreateRoute", "ec2:DeleteRoute",
      "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable",
      "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
      "ec2:CreateLaunchTemplate", "ec2:DeleteLaunchTemplate", "ec2:CreateLaunchTemplateVersion",
      "ec2:CreateTags", "ec2:DeleteTags",
    ]
    resources = ["*"]
  }

  # The strongest statement. PassRole is how a role hands an identity to
  # an AWS service — the classic escalation vector if left open. Here it
  # is double-locked: two exact role ARNs, AND passable to two services
  # only. It cannot be repurposed.
  statement {
    sid       = "PassClusterAndNodeRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.eks_cluster.arn, aws_iam_role.eks_node.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["eks.amazonaws.com", "ec2.amazonaws.com"]
    }
  }

  # Terraform reads these roles to compute a diff. Read-only, same two ARNs.
  statement {
    sid       = "ReadOwnRoles"
    actions   = ["iam:GetRole", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies"]
    resources = [aws_iam_role.eks_cluster.arn, aws_iam_role.eks_node.arn]
  }

  # AWS services create their own internal roles on first use. Resource
  # must be "*" (the role does not exist yet), so the condition does the
  # work — only these three services.
  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["eks.amazonaws.com", "eks-nodegroup.amazonaws.com", "autoscaling.amazonaws.com"]
    }
  }

  # Managed node groups create an Auto Scaling group on your behalf.
  # Terraform reads it during refresh. Reads and tags only.
  statement {
    sid       = "AutoScalingRead"
    actions   = ["autoscaling:Describe*", "autoscaling:CreateOrUpdateTags", "autoscaling:DeleteTags"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "platform_lab_deployer" {
  name   = "platform-lab-deployer-policy"
  policy = data.aws_iam_policy_document.deployer_permissions.json

  tags = {
    project = "platform-lab"
  }
}

resource "aws_iam_role_policy_attachment" "platform_lab_deployer" {
  role       = aws_iam_role.platform_lab_deployer.name
  policy_arn = aws_iam_policy.platform_lab_deployer.arn
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "deployer_role_arn" {
  value = aws_iam_role.platform_lab_deployer.arn
}

output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.github.arn
}

# ---------------------------------------------------------------------------
# IMPORT COMMANDS — run these once, before your first `terraform plan`,
# so Terraform adopts the existing resources instead of trying to create
# duplicates (which would fail since the names already exist).
#
#   terraform import \
#     aws_iam_openid_connect_provider.github \
#     arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com
#
#   terraform import \
#     aws_iam_role.platform_lab_deployer \
#     platform-lab-deployer
#
#   terraform import \
#     aws_iam_policy.platform_lab_deployer \
#     arn:aws:iam::<ACCOUNT_ID>:policy/platform-lab-deployer-policy
#
#   terraform import \
#     aws_iam_role_policy_attachment.platform_lab_deployer \
#     platform-lab-deployer/arn:aws:iam::<ACCOUNT_ID>:policy/platform-lab-deployer-policy
#
# After importing, run `terraform plan` — it should show NO changes if the
# console-created resources already match this file exactly. If it shows
# diffs, review carefully before applying (it means the console config and
# this file disagree on some detail, e.g. thumbprint or a missing action).
# ---------------------------------------------------------------------------
