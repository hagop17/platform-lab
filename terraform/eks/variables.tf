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
