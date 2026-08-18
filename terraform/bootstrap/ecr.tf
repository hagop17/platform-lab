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
