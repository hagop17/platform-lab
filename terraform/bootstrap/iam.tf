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

  tags = {
    project = "platform-lab"
  }
}

# ---------------------------------------------------------------------------
# Permissions policy — what the role can actually do
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "deployer_permissions" {
  statement {
    sid    = "TerraformState"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:DeleteObject",
    ]
    resources = [
      "arn:aws:s3:::${var.tfstate_bucket_name}",
      "arn:aws:s3:::${var.tfstate_bucket_name}/*",
    ]
  }

  statement {
    sid    = "TerraformLock"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [
      "arn:aws:dynamodb:us-west-2:${var.account_id}:table/${var.tflock_table_name}",
    ]
  }

  statement {
    sid    = "EC2Access"
    effect = "Allow"
    actions = [
      "ec2:RunInstances",
      "ec2:TerminateInstances",
      "ec2:Describe*",
      "ec2:CreateTags",
      "ec2:CreateSecurityGroup",
      "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:CreateVpc",
      "ec2:DeleteVpc",
      "ec2:CreateSubnet",
      "ec2:DeleteSubnet",
      "ec2:AttachInternetGateway",
      "ec2:CreateInternetGateway",
      "ec2:DeleteInternetGateway",
      "ec2:DetachInternetGateway",
      "ec2:CreateRouteTable",
      "ec2:DeleteRouteTable",
      "ec2:CreateRoute",
      "ec2:AssociateRouteTable",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ECRAccess"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:CreateRepository",
      "ecr:DescribeRepositories",
      "ecr:BatchGetImage",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "CloudWatchAccess"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DescribeAlarms",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
    ]
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
