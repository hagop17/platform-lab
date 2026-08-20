# EKS Cluster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the platform-lab app and Prometheus on a real EKS cluster that Terraform can create and destroy reliably, so the cluster exists only while in use.

**Architecture:** Three layers by lifetime. `terraform/bootstrap/` holds permanent identity and the image registry, applied only by `hagop-admin`. `terraform/eks/` holds everything billable — VPC, cluster, node group — destroyed after every session. `k8s/` holds plain Kubernetes manifests applied with `kubectl`, which die with the cluster and appear in no state file.

**Tech Stack:** Terraform ≥1.15 with AWS provider ~>5.0, raw resources (no community modules), EKS 1.36, `kubectl`, Docker, ECR.

**Spec:** [`docs/superpowers/specs/2026-08-14-eks-cluster-design.md`](../specs/2026-08-14-eks-cluster-design.md)

## Tasks at a glance

Tasks 1–11 change only the repo, cost nothing, and are verified offline. Tasks 12–14 run on the host against live AWS.

| # | Task | Deliverable | Gate |
|---|---|---|---|
| [1](#task-1-ecr-repository) | ECR repository | `ecr.tf` — registry, immutable tags, keep 3 | `validate` |
| [2](#task-2-eks-cluster-and-node-roles) | EKS cluster and node roles | `eks_roles.tf` — the two roles AWS assumes for you | `validate` |
| [3](#task-3-permissions-boundary-and-rewritten-deployer-policy) | **Permissions boundary + deployer policy** | `boundary.tf`, rewritten `iam.tf` — the security-critical task | `validate` |
| [4](#task-4-budget-alert) | Budget alert | `budget.tf` — $20 forecast alarm | `validate` |
| [5](#task-5-cluster-stack-scaffold) | Cluster stack scaffold | `terraform/eks/` — backend, provider, variables | `init -backend=false && validate` |
| [6](#task-6-network) | Network | `vpc.tf` — VPC, 2 subnets, IGW, routing | `validate` |
| [7](#task-7-cluster-node-group-access-entry) | Cluster, node group, access entry | `eks.tf`, `outputs.tf` — the cluster itself | `validate` |
| [8](#task-8-app-manifests) | App manifests | `k8s/app-*.yaml` — Deployment with three probes, Service | `kubeconform` |
| [9](#task-9-prometheus-manifests) | Prometheus manifests | `k8s/prometheus-*.yaml` — ConfigMap, Deployment, Service | `kubeconform` |
| [10](#task-10-ci-validation) | CI validation | One `terraform validate` step | CI run |
| [11](#task-11-documentation) | Documentation | `kubectl` prerequisite, roadmap bullets | — |
| | | | |
| [12](#task-12-apply-bootstrap-and-push-the-image--host) | **HOST** — apply bootstrap, push image | Live roles, live ECR, image in it | `terraform plan` shows no destroys |
| [13](#task-13-first-cycle--host) | **HOST** — first cycle (~$0.27) | apply → verify → destroy as admin | `list-clusters` empty |
| [14](#task-14-verify-under-least-privilege--host) | **HOST** — verify under least privilege (~$0.27) | Full cycle as the deployer role | The definition of done |

**The dependency that matters:** Tasks 1–2 produce IAM ARNs and an ECR URL that Tasks 3, 5, 7 and 12 consume. Everything else is independent, so 8–11 can be done in any order.

**Task 14 is the finish line**, not Task 13 — a cluster working under admin proves half the design.

## Global Constraints

- **Region is `us-west-2`** everywhere. No resource in another region.
- **Kubernetes version pinned to `1.36`.** Never unset, never latest-by-default.
- **`required_version = ">= 1.15"`, AWS provider `~> 5.0`** in every stack, matching bootstrap.
- **No account ID literal in any committed file.** It reaches Terraform through gitignored `terraform.tfvars` / `backend.hcl` only.
- **The invariant: everything that costs money lives in Terraform's state file.** No `type: LoadBalancer` Service, no PersistentVolumeClaim, no NAT gateway, no operator — each would create a billing AWS resource Terraform cannot see or delete.
- **`replicas: 1`** for both Deployments. The static Prometheus scrape target is only correct at one replica.
- **Services must be named exactly `app` and `prometheus`.** `prometheus.yml` and `metrics_analysis.py` resolve those names unchanged; renaming either breaks the app silently.
- **Namespace `default`.** No namespace is created.
- **`terraform/bootstrap/` is applied by `hagop-admin` only, permanently.** Never by the deployer role.
- **ECR: `IMMUTABLE` tags, keep the 3 most recent images.** Image tags are the git short SHA, never `latest`.
- **Tasks 1–11 touch only the repo and cost nothing. Tasks 12–14 run against live AWS and cost real money.**

## Testing approach — read before Task 1

There are no unit tests here, because there is no unit to test: Terraform describes cloud resources and manifests describe cluster objects. The equivalent fast feedback loop, used as the per-task gate:

| Artifact | Gate | Catches |
|---|---|---|
| Terraform | `terraform fmt -check`, then `terraform init -backend=false && terraform validate` | Formatting, syntax, undefined variables, wrong argument names, type errors |
| Manifests | `kubeconform -strict -summary k8s/*.yaml` | YAML syntax and Kubernetes schema errors, including unknown/misspelled fields |

Both run offline with **no AWS credentials** — which is why Tasks 1–11 are safe to iterate on freely. `-backend=false` is what lets `validate` run without touching S3.

> **Corrected during execution (2026-08-20):** the manifest gate was originally
> `kubectl apply --dry-run=client -f k8s/`, which does **not** run offline — client-side
> dry-run still fetches the API server's OpenAPI schema and API group list. See Task 8
> Step 3 for the full detail. `kubeconform` genuinely validates with no cluster, and is
> what Task 10 runs in CI. Neither tool can catch a Deployment/Service selector-label
> mismatch: that is schema-valid but semantically wrong.

The only real verification is Task 13's apply/verify/destroy cycle. Nothing before it proves the design works; it proves the code is well-formed.

## File structure

**Created:**

| File | Responsibility |
|---|---|
| `terraform/bootstrap/ecr.tf` | Image registry and its retention policy |
| `terraform/bootstrap/eks_roles.tf` | The two roles AWS assumes on your behalf |
| `terraform/bootstrap/boundary.tf` | The permissions boundary — the deployer's hard ceiling |
| `terraform/bootstrap/budget.tf` | Cost alarm |
| `terraform/eks/main.tf` | Terraform settings, S3 backend, AWS provider |
| `terraform/eks/variables.tf` | Inputs, including role ARNs from bootstrap |
| `terraform/eks/vpc.tf` | VPC, subnets, gateway, routing |
| `terraform/eks/eks.tf` | Cluster, node group, access entry |
| `terraform/eks/outputs.tf` | Cluster name, endpoint, kubeconfig command |
| `terraform/eks/backend.hcl.example` | Template for the gitignored backend config |
| `k8s/app-deployment.yaml` | How the app runs |
| `k8s/app-service.yaml` | Stable DNS name `app` |
| `k8s/prometheus-configmap.yaml` | `prometheus.yml`, carried into the cluster |
| `k8s/prometheus-deployment.yaml` | How Prometheus runs |
| `k8s/prometheus-service.yaml` | Stable DNS name `prometheus` |

**Modified:** `terraform/bootstrap/iam.tf` (identity policy replaced; boundary and session duration added), `terraform/bootstrap/terraform.tfvars.example`, `.gitignore`, `.github/workflows/ci.yml`, `CLAUDE.md`, `README.md`.

**Why new files rather than growing `iam.tf`:** it is already 275 lines. Adding a registry, two roles, a boundary and a budget would push it past 500 and mix four unrelated concerns. `iam.tf` keeps only the deployer identity — trust, permissions, boundary attachment.

---

### Task 1: ECR repository

**Files:**
- Create: `terraform/bootstrap/ecr.tf`

**Interfaces:**
- Consumes: nothing
- Produces: output `ecr_repository_url` (string) — used by Task 8's `image:` field and Task 12's `docker push`

- [ ] **Step 1: Create `terraform/bootstrap/ecr.tf`**

```hcl
# ecr.tf
# The image registry. Lives in bootstrap, not terraform/eks/, because the
# image is ~2 GB and rebuilding + pushing costs ~15 minutes. A
# `terraform destroy` of the cluster must never delete it.

resource "aws_ecr_repository" "platform_lab" {
  name = "platform-lab"

  # Tags are the git short SHA, so a tag must never be reassigned to
  # different bytes — that would make "what is deployed" unanswerable.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    project = "platform-lab"
  }
}

# Without this, every push accumulates forever — ~2 GB each.
resource "aws_ecr_lifecycle_policy" "platform_lab" {
  repository = aws_ecr_repository.platform_lab.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the 3 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = {
        type = "expire"
      }
    }]
  })
}

output "ecr_repository_url" {
  value       = aws_ecr_repository.platform_lab.repository_url
  description = "Push target for the app image; also the k8s Deployment image prefix"
}
```

- [ ] **Step 2: Verify**

```bash
cd terraform/bootstrap
terraform fmt -check
terraform validate
```

Expected: `fmt -check` prints nothing (exit 0); `validate` prints `Success! The configuration is valid.`

If `validate` reports the configuration is not initialised, run `terraform init -backend=false` first.

- [ ] **Step 3: Commit**

```bash
git add terraform/bootstrap/ecr.tf
git commit -m "Add ECR repository for the app image"
```

---

### Task 2: EKS cluster and node roles

**Files:**
- Create: `terraform/bootstrap/eks_roles.tf`

**Interfaces:**
- Consumes: nothing
- Produces: `aws_iam_role.eks_cluster.arn`, `aws_iam_role.eks_node.arn`, and matching outputs — consumed by Task 3's `iam:PassRole` statement and Task 5's variables

- [ ] **Step 1: Create `terraform/bootstrap/eks_roles.tf`**

```hcl
# eks_roles.tf
# Two roles AWS itself assumes on your behalf. They live in bootstrap so
# the deployer role never needs iam:CreateRole — which, combined with
# iam:AttachRolePolicy, is a privilege-escalation primitive.

# --- Cluster role: assumed by the EKS control plane ----------------------

data "aws_iam_policy_document" "eks_cluster_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_cluster" {
  name               = "platform-lab-eks-cluster"
  description        = "Assumed by the EKS control plane"
  assume_role_policy = data.aws_iam_policy_document.eks_cluster_assume.json

  tags = {
    project = "platform-lab"
  }
}

resource "aws_iam_role_policy_attachment" "eks_cluster" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# --- Node role: assumed by the EC2 instances in the node group ----------

data "aws_iam_policy_document" "eks_node_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_node" {
  name               = "platform-lab-eks-node"
  description        = "Assumed by EKS worker nodes"
  assume_role_policy = data.aws_iam_policy_document.eks_node_assume.json

  tags = {
    project = "platform-lab"
  }
}

# AmazonEC2ContainerRegistryReadOnly is what makes ECR pulls work with no
# imagePullSecret in the Deployment.
resource "aws_iam_role_policy_attachment" "eks_node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])

  role       = aws_iam_role.eks_node.name
  policy_arn = each.value
}

output "eks_cluster_role_arn" {
  value = aws_iam_role.eks_cluster.arn
}

output "eks_node_role_arn" {
  value = aws_iam_role.eks_node.arn
}
```

- [ ] **Step 2: Verify**

```bash
cd terraform/bootstrap
terraform fmt -check && terraform validate
```

Expected: both succeed.

- [ ] **Step 3: Commit**

```bash
git add terraform/bootstrap/eks_roles.tf
git commit -m "Add EKS cluster and node IAM roles"
```

---

### Task 3: Permissions boundary and rewritten deployer policy

The security-critical task. Two artifacts: a boundary capping what the deployer could ever do, and a replacement identity policy for what it does today.

**Files:**
- Create: `terraform/bootstrap/boundary.tf`
- Modify: `terraform/bootstrap/iam.tf` — replace `data "aws_iam_policy_document" "deployer_permissions"`, add two arguments to `aws_iam_role.platform_lab_deployer`, add a `cluster_name` variable

**Interfaces:**
- Consumes: `aws_iam_role.eks_cluster.arn`, `aws_iam_role.eks_node.arn` (Task 2)
- Produces: a deployer role able to create and destroy the Task 5–7 stack

- [ ] **Step 1: Create `terraform/bootstrap/boundary.tf`**

```hcl
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
      "iam:PassRole",
      "iam:GetRole",
      "iam:ListRole*",
      "iam:ListAttachedRolePolicies",
      "iam:CreateServiceLinkedRole",
    ]
    resources = ["*"]
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
```

- [ ] **Step 2: Attach the boundary and extend the session, in `iam.tf`**

Replace the `aws_iam_role.platform_lab_deployer` resource with:

```hcl
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
```

- [ ] **Step 3: Add a `cluster_name` variable to `iam.tf`**

```hcl
variable "cluster_name" {
  description = "EKS cluster name; scopes the deployer's eks:* write permissions"
  type        = string
  default     = "platform-lab"
}
```

- [ ] **Step 4: Replace the deployer identity policy in `iam.tf`**

Replace the entire `data "aws_iam_policy_document" "deployer_permissions"` block with:

```hcl
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
```

Note what disappears: `ec2:RunInstances`, `ec2:TerminateInstances`, the whole `ECRAccess` block, and `CloudWatchAccess`. Removing dead grants is as much the deliverable as adding needed ones.

- [ ] **Step 5: Verify**

```bash
cd terraform/bootstrap
terraform fmt -check && terraform validate
```

Expected: both succeed. A failure naming `aws_iam_role.eks_cluster` means Task 2 was skipped.

- [ ] **Step 6: Commit**

```bash
git add terraform/bootstrap/boundary.tf terraform/bootstrap/iam.tf
git commit -m "Add deployer permissions boundary and scope its identity policy to EKS"
```

---

### Task 4: Budget alert

**Files:**
- Create: `terraform/bootstrap/budget.tf`
- Modify: `terraform/bootstrap/iam.tf` (variable), `terraform/bootstrap/terraform.tfvars.example`

**Interfaces:**
- Consumes: nothing
- Produces: nothing consumed by later tasks

- [ ] **Step 1: Create `terraform/bootstrap/budget.tf`**

```hcl
# budget.tf
# A backstop, not the primary control. AWS billing data lags 8-24 hours,
# so this catches "forgot for days" and never "forgot overnight." The real
# check is `aws eks list-clusters` at the end of every session.
#
# FORECASTED rather than ACTUAL: it fires when AWS projects an overrun,
# which is earlier than waiting for spend to land.

resource "aws_budgets_budget" "monthly" {
  name         = "platform-lab-monthly"
  budget_type  = "COST"
  limit_amount = "20"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
```

- [ ] **Step 2: Add the variable to `iam.tf`**

```hcl
variable "budget_alert_email" {
  description = "Address notified when forecast spend exceeds 80% of the monthly budget"
  type        = string
}
```

- [ ] **Step 3: Document it in `terraform.tfvars.example`**

Append:

```hcl
budget_alert_email = "you@example.com"
```

- [ ] **Step 4: Verify**

```bash
cd terraform/bootstrap
terraform fmt -check && terraform validate
```

Expected: both succeed.

- [ ] **Step 5: Commit**

```bash
git add terraform/bootstrap/budget.tf terraform/bootstrap/iam.tf terraform/bootstrap/terraform.tfvars.example
git commit -m "Add monthly budget alert"
```

---

### Task 5: Cluster stack scaffold

**Files:**
- Create: `terraform/eks/main.tf`, `terraform/eks/variables.tf`, `terraform/eks/backend.hcl.example`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: bootstrap outputs `eks_cluster_role_arn`, `eks_node_role_arn` (Task 2), passed in as variables
- Produces: `var.cluster_name`, `var.k8s_version`, `var.instance_type`, `var.eks_cluster_role_arn`, `var.eks_node_role_arn`, `var.admin_principal_arn` for Tasks 6–7

- [ ] **Step 1: Create `terraform/eks/main.tf`**

```hcl
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
```

- [ ] **Step 2: Create `terraform/eks/variables.tf`**

```hcl
variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "platform-lab"
}

variable "k8s_version" {
  description = "Kubernetes version, pinned so a cluster recreated months later matches today's. Older versions drop into extended support, which costs ~6x for the control plane."
  type        = string
  default     = "1.36"
}

variable "instance_type" {
  description = "Node instance type. t3.large gives ~7 GiB allocatable — enough for both pods plus the brief two-pod overlap during a rolling update."
  type        = string
  default     = "t3.large"
}

variable "eks_cluster_role_arn" {
  description = "Cluster role ARN, output by terraform/bootstrap/"
  type        = string
}

variable "eks_node_role_arn" {
  description = "Node role ARN, output by terraform/bootstrap/"
  type        = string
}

variable "admin_principal_arn" {
  description = "IAM principal granted cluster-admin kubectl access. Must be explicit: EKS otherwise grants access only to whoever created the cluster, which differs between the admin and deployer runs."
  type        = string
}
```

- [ ] **Step 3: Create `terraform/eks/backend.hcl.example`**

```hcl
# Copy to backend.hcl (gitignored), fill in the account ID, then:
#   terraform init -backend-config=backend.hcl
bucket = "platform-lab-tfstate-<ACCOUNT_ID>-us-west-2-an"
```

- [ ] **Step 4: Ignore the real backend config**

Add to `.gitignore`, under the existing Terraform section:

```
backend.hcl
```

- [ ] **Step 5: Verify**

```bash
cd terraform/eks
terraform init -backend=false
terraform fmt -check && terraform validate
```

Expected: `init` succeeds without contacting S3; `validate` prints `Success!`.

- [ ] **Step 6: Commit**

```bash
git add terraform/eks/main.tf terraform/eks/variables.tf terraform/eks/backend.hcl.example .gitignore
git commit -m "Add cluster stack scaffold with S3 backend"
```

---

### Task 6: Network

**Files:**
- Create: `terraform/eks/vpc.tf`

**Interfaces:**
- Consumes: `var.cluster_name` (Task 5)
- Produces: `aws_subnet.public[*].id` — consumed by Task 7's cluster and node group

- [ ] **Step 1: Create `terraform/eks/vpc.tf`**

```hcl
# vpc.tf
# A throwaway network, destroyed with the cluster. Public subnets and no
# NAT gateway: nodes get public IPs and the free internet gateway does the
# 1:1 translation. A NAT gateway would cost ~$0.045/hr and — more
# importantly — NAT gateways and their Elastic IPs are among the most
# common causes of a `terraform destroy` that hangs on "VPC has
# dependencies". Nothing is exposed inbound; access is kubectl
# port-forward through the EKS API server.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"

  # Both are required for the cluster's private endpoint to resolve inside
  # the VPC. enable_dns_hostnames defaults to FALSE on a non-default VPC;
  # without it, nodes silently cannot reach the control plane.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.cluster_name}-vpc"
  }
}

# Two subnets in two availability zones is mandatory: EKS refuses to
# create a cluster whose subnets span fewer than two, even with one node.
resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.${count.index + 1}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.cluster_name}-public-${count.index + 1}"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.cluster_name}-igw"
  }
}

# A subnet is "public" purely because its route table points at an
# internet gateway. There is no public/private flag in AWS.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.cluster_name}-public"
  }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
```

- [ ] **Step 2: Verify**

```bash
cd terraform/eks
terraform fmt -check && terraform validate
```

Expected: both succeed.

- [ ] **Step 3: Commit**

```bash
git add terraform/eks/vpc.tf
git commit -m "Add VPC with two public subnets"
```

---

### Task 7: Cluster, node group, access entry

**Files:**
- Create: `terraform/eks/eks.tf`, `terraform/eks/outputs.tf`

**Interfaces:**
- Consumes: `aws_subnet.public[*].id` (Task 6), all variables from Task 5
- Produces: outputs `cluster_name`, `cluster_endpoint`, `update_kubeconfig_command` — used in Task 13

- [ ] **Step 1: Create `terraform/eks/eks.tf`**

```hcl
# eks.tf
# The cluster itself. The classic raw-EKS depends_on hazard — a node group
# created before its role's policy attachments exist, producing nodes that
# never join — cannot happen here: those attachments live in
# terraform/bootstrap/ and were applied long before this stack runs.

resource "aws_eks_cluster" "main" {
  name     = var.cluster_name
  version  = var.k8s_version
  role_arn = var.eks_cluster_role_arn

  vpc_config {
    subnet_ids = aws_subnet.public[*].id

    # One API server, one hostname; these two settings control only how
    # that hostname resolves. Public: reachable from the host for kubectl.
    # Private: EKS publishes a private DNS record inside the VPC so
    # node -> control-plane traffic never leaves it. Both are free.
    endpoint_public_access  = true
    endpoint_private_access = true
  }

  access_config {
    authentication_mode = "API"

    # Deliberately false. EKS otherwise grants cluster-admin to whichever
    # principal created the cluster — admin in one phase, the deployer in
    # another — making kubectl access depend on who ran apply. The
    # explicit access entry below is deterministic instead.
    bootstrap_cluster_creator_admin_permissions = false
  }
}

# Without this, `kubectl get nodes` returns "error: You must be logged in
# to the server" after a deployer-created cluster.
resource "aws_eks_access_entry" "admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.admin_principal_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.admin_principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.admin]
}

# On-demand rather than spot: spot saves ~$0.058/hr (~$8/year at this
# usage) but AWS can reclaim the node on two minutes' notice. Not worth an
# extra failure mode on a cluster used for learning.
resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "default"
  node_role_arn   = var.eks_node_role_arn
  subnet_ids      = aws_subnet.public[*].id

  instance_types = [var.instance_type]
  capacity_type  = "ON_DEMAND"
  ami_type       = "AL2023_x86_64_STANDARD"
  disk_size      = 20

  scaling_config {
    desired_size = 1
    min_size     = 1
    max_size     = 1
  }

  update_config {
    max_unavailable = 1
  }
}
```

- [ ] **Step 2: Create `terraform/eks/outputs.tf`**

```hcl
output "cluster_name" {
  value = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.main.endpoint
}

# Must be run after EVERY apply, not just the first: a recreated cluster
# has a new endpoint and new certificate data, so a stale kubeconfig
# points at a cluster that no longer exists.
output "update_kubeconfig_command" {
  value = "aws eks update-kubeconfig --name ${aws_eks_cluster.main.name} --region us-west-2"
}
```

- [ ] **Step 3: Verify**

```bash
cd terraform/eks
terraform fmt -check && terraform validate
```

Expected: both succeed. If `aws_eks_access_entry` is reported as an unsupported resource type, the AWS provider is older than 5.33 — run `terraform init -backend=false -upgrade`.

- [ ] **Step 4: Commit**

```bash
git add terraform/eks/eks.tf terraform/eks/outputs.tf
git commit -m "Add EKS cluster, node group and admin access entry"
```

---

### Task 8: App manifests

**Files:**
- Create: `k8s/app-deployment.yaml`, `k8s/app-service.yaml`

**Interfaces:**
- Consumes: `ecr_repository_url` (Task 1); the Secret `platform-lab-secrets`, created by hand in Task 13
- Produces: Service DNS name `app`, ports 8000 and 9464 — consumed by Task 9's scrape config

**Requirements**, if you would rather write these from the spec first and diff against the YAML below:

- `replicas: 1`
- `image:` = the ECR URL with a **git short SHA** tag, never `latest`
- env `LLM_PROVIDER=groq`, `TPR_RAG_DATA_DIR=/home/appuser/.tpr-rag/chroma_data`
- **`PROMETHEUS_URL` must NOT be set** — `metrics_analysis.py` defaults to `http://prometheus:9090`, which resolves via the Service name
- `envFrom` the Secret `platform-lab-secrets`, marked optional so the pod schedules before the Secret exists
- ports 8000 (app) and 9464 (metrics)
- requests `1Gi` / `500m`, limits `2Gi`
- **all three probes**, on `/health`
- Service named exactly **`app`**, type ClusterIP

- [ ] **Step 1: Create `k8s/app-deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
  labels:
    app: platform-lab
spec:
  replicas: 1
  selector:
    matchLabels:
      app: platform-lab
  template:
    metadata:
      labels:
        app: platform-lab
    spec:
      containers:
        - name: app
          # Replace the account ID and tag with the pushed git short SHA.
          image: ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/platform-lab:REPLACE_WITH_GIT_SHA
          ports:
            - name: http
              containerPort: 8000
            - name: metrics
              containerPort: 9464
          env:
            - name: LLM_PROVIDER
              value: groq
            - name: TPR_RAG_DATA_DIR
              value: /home/appuser/.tpr-rag/chroma_data
          envFrom:
            - secretRef:
                name: platform-lab-secrets
                optional: true
          resources:
            requests:
              memory: 1Gi
              cpu: 500m
            limits:
              memory: 2Gi
          # main.py imports rag.router -> rag/tpr_rag.py, which constructs
          # SentenceTransformer and the ChromaDB client at MODULE level, so
          # startup takes seconds to tens of seconds. Without a
          # startupProbe the liveness probe kills the container mid-boot,
          # forever, in a crash loop that reads as an application bug.
          startupProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 5
            failureThreshold: 30 # 150s budget
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 10
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 5
```

- [ ] **Step 2: Create `k8s/app-service.yaml`**

```yaml
# The name `app` is load-bearing: prometheus.yml scrapes "app:9464"
# unchanged from the Compose setup, because Kubernetes Service DNS matches
# Compose service DNS. Renaming this breaks scraping silently.
apiVersion: v1
kind: Service
metadata:
  name: app
spec:
  type: ClusterIP
  selector:
    app: platform-lab
  ports:
    - name: http
      port: 8000
      targetPort: 8000
    - name: metrics
      port: 9464
      targetPort: 9464
```

- [ ] **Step 3: Verify schema**

```bash
kubeconform -strict -summary k8s/app-deployment.yaml k8s/app-service.yaml
```

Expected: `Valid: 2, Invalid: 0, Errors: 0`.

> **Corrected during execution (2026-08-20).** This step originally read
> `kubectl apply --dry-run=client -f ...`, claiming "no cluster is needed —
> `--dry-run=client` validates locally." **That is wrong.** On kubectl
> v1.36.3, client-side dry-run still contacts the API server: it downloads
> the OpenAPI schema and resolves the API group list, so with no cluster it
> fails with `failed to download openapi: ... connection refused` — and
> `--validate=false` fails too, with `unable to recognize ...`. Offline, the
> only thing it catches is raw YAML parse errors. `kubeconform` is a genuine
> offline schema validator and is what Task 10 wires into CI.

- [ ] **Step 4: Commit**

```bash
git add k8s/app-deployment.yaml k8s/app-service.yaml
git commit -m "Add app Deployment and Service manifests"
```

---

### Task 9: Prometheus manifests

**Files:**
- Create: `k8s/prometheus-configmap.yaml`, `k8s/prometheus-deployment.yaml`, `k8s/prometheus-service.yaml`

**Interfaces:**
- Consumes: Service DNS name `app` (Task 8)
- Produces: Service DNS name `prometheus` on port 9090 — what makes `metrics_analysis.py`'s default URL resolve

- [ ] **Step 1: Create `k8s/prometheus-configmap.yaml`**

A ConfigMap is the Kubernetes equivalent of Compose's bind mount: the node has never seen your repo, so the file must be carried into the cluster. The `data` key reproduces `prometheus.yml` **verbatim**.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-config
data:
  # Verbatim copy of ./prometheus.yml. Known wart: this duplicates the
  # file, and the two can drift. Kustomize's configMapGenerator would
  # remove the duplication and is a natural follow-up.
  prometheus.yml: |
    global:
      scrape_interval: 5s

    scrape_configs:
      - job_name: "platform-lab"
        static_configs:
          - targets: ["app:9464"]
```

- [ ] **Step 2: Create `k8s/prometheus-deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prometheus
  labels:
    app: prometheus
spec:
  replicas: 1
  selector:
    matchLabels:
      app: prometheus
  template:
    metadata:
      labels:
        app: prometheus
    spec:
      containers:
        - name: prometheus
          image: prom/prometheus:latest
          ports:
            - name: http
              containerPort: 9090
          volumeMounts:
            - name: config
              mountPath: /etc/prometheus
            - name: data
              mountPath: /prometheus
          resources:
            requests:
              memory: 256Mi
            limits:
              memory: 512Mi
          livenessProbe:
            httpGet:
              path: /-/healthy
              port: 9090
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /-/ready
              port: 9090
            periodSeconds: 5
      volumes:
        - name: config
          configMap:
            name: prometheus-config
        # emptyDir, NOT a PersistentVolumeClaim. A PVC would make
        # Kubernetes provision an EBS volume that Terraform never sees and
        # `destroy` never deletes — breaking the invariant that everything
        # billable lives in Terraform's state. The data is a few minutes of
        # demo metrics; losing it is the point.
        - name: data
          emptyDir: {}
```

- [ ] **Step 3: Create `k8s/prometheus-service.yaml`**

```yaml
# The name `prometheus` is load-bearing: metrics_analysis.py defaults to
# http://prometheus:9090, so this Service is what makes that default
# resolve with no code change.
apiVersion: v1
kind: Service
metadata:
  name: prometheus
spec:
  type: ClusterIP
  selector:
    app: prometheus
  ports:
    - name: http
      port: 9090
      targetPort: 9090
```

- [ ] **Step 4: Verify the whole directory**

```bash
kubeconform -strict -summary k8s/*.yaml
```

Expected: `5 resources found in 5 files - Valid: 5, Invalid: 0, Errors: 0`.
(Originally `kubectl apply --dry-run=client -f k8s/` — see Task 8 Step 3 for why that
does not work without a cluster.)

- [ ] **Step 5: Confirm the ConfigMap matches the real file**

```bash
diff <(sed -n '/prometheus.yml: |/,$p' k8s/prometheus-configmap.yaml | tail -n +2 | sed 's/^    //') prometheus.yml
```

Expected: no output. Any difference means the two copies have drifted.

- [ ] **Step 6: Commit**

```bash
git add k8s/prometheus-configmap.yaml k8s/prometheus-deployment.yaml k8s/prometheus-service.yaml
git commit -m "Add Prometheus manifests"
```

---

### Task 10: CI validation

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `terraform/eks/` (Tasks 5–7)
- Produces: nothing

- [ ] **Step 1: Add a validate step after the existing `terraform fmt` step**

The workflow already runs `terraform fmt -check -recursive terraform`, so formatting is covered. Add immediately after it:

```yaml
      # -backend=false skips S3, so this needs no AWS credentials —
      # consistent with the repo convention that CI never reaches the
      # network for state.
      - name: Terraform validate
        run: |
          terraform -chdir=terraform/eks init -backend=false
          terraform -chdir=terraform/eks validate
```

- [ ] **Step 2: Verify the workflow parses**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "Validate the EKS stack in CI"
```

---

### Task 11: Documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: nothing
- Produces: nothing

- [ ] **Step 1: Add `kubectl` to CLAUDE.md's "Toolchain & commands"**

```markdown
- **Kubernetes CLI:** `kubectl` — required on the host for the EKS deployment (see `docs/superpowers/specs/2026-08-14-eks-cluster-design.md`). Not installed in the dev container, which has no AWS credentials. Keep it within ±1 minor version of the cluster (currently 1.36).
```

- [ ] **Step 2: Update the README roadmap bullets**

Replace:

```markdown
- [ ] Terraform to provision the stack
- [ ] Kubernetes manifests / Helm chart
```

with:

```markdown
- [x] Terraform to provision the stack (`terraform/bootstrap/`, `terraform/eks/`)
- [x] Kubernetes manifests (`k8s/`)
```

Leave the Grafana and OTel Collector bullets unchecked — both are explicitly out of scope.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "Document kubectl prerequisite and update roadmap"
```

---

### Task 12: Apply bootstrap and push the image — HOST

**First task that touches AWS.** Costs ~$0 (ECR storage only). Everything here runs on the host; the dev container has no AWS credentials.

**Files:** `k8s/app-deployment.yaml` (image reference)

**Interfaces:**
- Consumes: Tasks 1–4, 8
- Produces: live IAM roles, a live ECR repo, and an image in it — required by Tasks 13–14

- [ ] **Step 1: Authenticate as admin**

```bash
aws login --profile hagop-admin
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_PROFILE=hagop-admin-tf
aws sts get-caller-identity
```

Expected: the identity shows `hagop-admin`.

> **Corrected during execution (2026-08-20).** This step originally used
> `eval "$(aws configure export-credentials --profile hagop-admin --format env)"`.
> **Do not use that pattern for anything long-running.** `aws login` issues
> credentials that expire every **15 minutes** and are auto-refreshed by the
> CLI/SDKs for up to 12 hours; the `eval` freezes one 15-minute snapshot into
> env vars that nothing can renew. It killed four consecutive apply/destroy
> runs mid-flight with `ExpiredToken`, twice leaving a live cluster untracked
> in state. The 15-minute lifetime is not configurable — there is no duration
> flag on `aws login`.
>
> The fix is a `credential_process` profile, which lets the SDK re-invoke the
> command as expiry nears, so refresh happens transparently mid-run. Add once
> to `~/.aws/config`:
>
> ```ini
> [profile hagop-admin-tf]
> credential_process = aws configure export-credentials --profile hagop-admin
> region = us-west-2
> ```
>
> The `unset` is mandatory, not tidiness: env vars outrank profiles in the AWS
> credential chain, so a stale snapshot would silently win over the profile.

- [ ] **Step 2: Add the new variable, then plan**

Add `budget_alert_email` to `terraform/bootstrap/terraform.tfvars` (gitignored), then:

```bash
cd terraform/bootstrap
terraform plan
```

Expected: **add** ECR repo + lifecycle policy, 2 roles + 4 policy attachments, boundary policy, budget; **change** the deployer role (boundary, session duration) and its policy. **No destroys.**

If any destroy appears, stop and investigate before applying.

- [ ] **Step 3: Apply**

```bash
terraform apply
terraform output
```

Note `ecr_repository_url`, `eks_cluster_role_arn`, `eks_node_role_arn` — Task 13 needs all three.

- [ ] **Step 4: Build and push the image**

```bash
cd ../..
SHA=$(git rev-parse --short HEAD)
ECR=$(terraform -chdir=terraform/bootstrap output -raw ecr_repository_url)

aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin "${ECR%%/*}"

docker build -t "$ECR:$SHA" .
docker push "$ECR:$SHA"
```

Expected: push completes. The image is ~2 GB, so allow time on a slow uplink.

- [ ] **Step 5: Substitute the real image reference at apply time — do NOT commit it**

Leave the `ACCOUNT_ID` / `REPLACE_WITH_GIT_SHA` placeholders in
`k8s/app-deployment.yaml` exactly as committed. Substitute them only in the
stream handed to `kubectl`, so the real values never reach a tracked file:

```bash
kubeconform -strict k8s/app-deployment.yaml
sed "s|ACCOUNT_ID|${ECR%%.*}|; s|REPLACE_WITH_GIT_SHA|$SHA|" \
  k8s/app-deployment.yaml | kubectl apply -f -
```

> **Corrected during execution (2026-08-20).** This step originally read
> "Edit `k8s/app-deployment.yaml`, replacing the placeholder `image:` line
> with the actual `$ECR:$SHA` value", followed by `git add` and `git commit`.
> That **contradicts this plan's own global constraint** — *"No account ID
> literal in any committed file"* — because the ECR URL embeds the account ID
> (`<ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/...`). Following it would
> silently reintroduce into git exactly what the rest of the design keeps out
> via gitignored `terraform.tfvars` / `backend.hcl`. The placeholders stay
> committed permanently; substitution happens at deploy time only.

---

### Task 13: First cycle — HOST

**Costs ~$0.27.** Proves the design works and, more importantly, that teardown is clean.

**Files:** none

**Interfaces:**
- Consumes: Task 12
- Produces: a validated apply/destroy cycle

- [ ] **Step 1: Configure the backend**

```bash
cd terraform/eks
cp backend.hcl.example backend.hcl
# edit backend.hcl, substituting the real account ID
terraform init -backend-config=backend.hcl
```

If `init` warns that `dynamodb_table` is deprecated, or you prefer S3-native locking, add `use_lockfile = true` to the `backend "s3"` block and re-run. This is the open question in spec §13; the first `init` answers it.

- [ ] **Step 2: Apply**

```bash
terraform apply \
  -var="eks_cluster_role_arn=$(terraform -chdir=../bootstrap output -raw eks_cluster_role_arn)" \
  -var="eks_node_role_arn=$(terraform -chdir=../bootstrap output -raw eks_node_role_arn)" \
  -var="admin_principal_arn=$(aws sts get-caller-identity --query Arn --output text)"
```

Expected: ~18 minutes; the cluster alone takes 12–15.

Consider putting those three values in a gitignored `terraform.tfvars` to avoid retyping them each session.

- [ ] **Step 3: Deploy the workload**

```bash
aws eks update-kubeconfig --name platform-lab --region us-west-2
kubectl create secret generic platform-lab-secrets --from-env-file=../../.env
kubectl apply -f ../../k8s/
kubectl rollout status deployment/app --timeout=5m
```

Expected: rollout completes. It waits out the ~150s model load — that is the `startupProbe` working, not a hang.

- [ ] **Step 4: Verify**

```bash
kubectl get nodes                 # 1 node, Ready
kubectl get pods                  # 2 pods, Running, 0 restarts
kubectl port-forward svc/app 8000:8000 &
kubectl port-forward svc/prometheus 9090:9090 &

curl localhost:8000/health
for i in {1..5}; do curl -s localhost:8000/work > /dev/null; done
sleep 10
curl -s 'localhost:9090/api/v1/query?query=up{job="platform-lab"}'
```

Expected: `/health` responds; the final query returns a result containing `"1"`.

The `/work` loop matters — Prometheus only has data if traffic happened. Hitting a route does not trigger a scrape.

- [ ] **Step 5: Tear down and confirm**

```bash
kill %1 %2
terraform destroy   # same -var flags as apply

aws eks list-clusters --region us-west-2
aws ec2 describe-instances --region us-west-2 \
  --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId'
```

Expected: destroy completes in ~10 minutes; both queries return empty.

**No `kubectl delete` first** — the objects die with the cluster.

If destroy hangs on "VPC has dependencies", something created an AWS resource outside Terraform's knowledge — look for load balancers and network interfaces in the VPC. That should be impossible given the invariant, and finding one means the invariant was broken.

---

### Task 14: Verify under least privilege — HOST

**The real finish line.** A cluster working under admin proves half the design.

**Files:** possibly `terraform/bootstrap/iam.tf`, if gaps are found

**Interfaces:**
- Consumes: Task 13
- Produces: a deployer role proven sufficient for a full lifecycle

- [ ] **Step 1: Assume the deployer role in a second shell**

Shell 2:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_PROFILE=platform-lab-deployer
aws sts get-caller-identity
```

Expected: the ARN shows `.../platform-lab-deployer/...`.

> **Corrected during execution (2026-08-20).** This step originally chained
> `eval "$(aws configure export-credentials --format env)"` into a hand-rolled
> `aws sts assume-role` piped through `export`. Both halves freeze static credentials
> that nothing can renew — fatal here, because this task runs a full apply **and**
> destroy (~30-40 min) against `aws login` credentials that expire every 15 minutes.
> Use the named profile instead; it requires `[profile platform-lab-deployer]` to set
> `source_profile = hagop-admin-tf` (see the design spec's "two shells" section). The
> SDK then auto-refreshes the role assumption against the deployer's 4-hour
> `max_session_duration`.

- [ ] **Step 2: Apply as the deployer**

In shell 2:

```bash
cd terraform/eks && terraform apply   # same -var flags
```

If this fails with `AccessDenied`, go to Step 4.

- [ ] **Step 3: Deploy and verify from shell 1**

`kubectl` must run in **shell 1** (admin). As the deployer it would authenticate as a principal with no access entry and be refused — that is the design working, not a bug.

Repeat Task 13 Steps 3–4.

- [ ] **Step 4: Close any permission gaps**

Find exactly what was denied:

```bash
aws cloudtrail lookup-events --start-time <apply start> --end-time <now> \
  --query 'Events[].[CloudTrailEvent]' --output text \
  | jq -r 'fromjson | select(.errorCode != null)
           | "\(.errorCode)  \(.eventSource) \(.eventName)"' | sort -u
```

This returns the denied calls and nothing else. Add exactly those actions to the relevant statement in `terraform/bootstrap/iam.tf`, apply bootstrap **as admin in shell 1**, and re-run Step 2. Two or three iterations is normal, ~25 minutes and ~$0.10 each.

Do not widen to `eks:*` or `ec2:*` to make it stop — that discards the deliverable. Note CloudTrail lags 5–15 minutes.

- [ ] **Step 5: Destroy as the deployer**

In shell 2:

```bash
terraform destroy   # same -var flags
aws eks list-clusters --region us-west-2
```

Expected: empty.

Destroy exercises different permissions than apply — `Delete*`, `Detach*`, `DeleteTags`. A policy that applies successfully and cannot destroy is the specific failure this design exists to prevent, so this step is not optional.

- [ ] **Step 6: Commit any policy changes**

```bash
git add terraform/bootstrap/iam.tf
git commit -m "Close deployer policy gaps found during verification"
```

- [ ] **Step 7: Confirm the definition of done**

All of: bootstrap applied; image pushed; apply as admin worked; verification passed; destroy clean; **a full apply → verify → destroy cycle completed as `platform-lab-deployer`**; docs updated.

---

## Notes for whoever executes this

**Tasks 1–11 cost nothing and are fully reversible.** Iterate freely — the gates are offline and instant.

**Tasks 12–14 spend real money** (~$0.19/hr while a cluster exists). End every session with `aws eks list-clusters --region us-west-2` and confirm it returns empty. The budget alert is a backstop that lags 8–24 hours, not a safety net for the same evening.

**Two shells, one identity each**, in Tasks 13–14: `hagop-admin` runs bootstrap, `kubectl`, and image pushes; `platform-lab-deployer` runs `terraform/eks/` apply and destroy. Mixing them produces confusing failures.

**If something must change mid-session**, edit a manifest and `kubectl apply` — seconds — rather than destroying and recreating infrastructure, which is 25 minutes and real money.
