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
