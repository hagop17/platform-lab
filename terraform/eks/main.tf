# main.tf
# The ephemeral stack. Everything here is created and destroyed every
# session — this is the only directory `terraform destroy` ever runs in.
#
# Exactly one provider, `aws`. That is deliberate: Terraform must
# initialise every provider before it can plan anything, and a
# `kubernetes` provider configured from the cluster below would fail to
# initialise once that cluster is gone — making `terraform destroy`
# unplannable. Kubernetes objects are applied with kubectl instead.

terraform {
  required_version = ">= 1.15"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Partial configuration — the bucket name embeds the account ID, so it
  # is supplied at init time from a gitignored file rather than committed:
  #
  #   terraform init -backend-config=backend.hcl
  #
  backend "s3" {
    key    = "eks/terraform.tfstate"
    region = "us-west-2"
  }
}

provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = {
      project = "platform-lab"
    }
  }
}
