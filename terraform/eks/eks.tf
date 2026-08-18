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
