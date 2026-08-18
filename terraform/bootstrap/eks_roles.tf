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
