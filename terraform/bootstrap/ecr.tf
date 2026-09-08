# ecr.tf
# The image registry. Lives in bootstrap, not terraform/eks/, because
# rebuilding + pushing the image costs ~15 minutes (552 MB compressed, and
# the build re-downloads the embedding model and rebuilds the RAG index). A
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

# Without this, every push accumulates forever — 552 MB each.
#
# "3 images" means 3 MANIFESTS, and tagStatus "any" counts untagged ones.
# That makes this rule's correctness depend on a docker build flag: BuildKit
# attaches a provenance attestation by default, which forces the result into
# an OCI image index, so one default push lands as three manifests (index +
# image + attestation) and this silently becomes "keep one." It can also
# expire an untagged child manifest while the tagged index still references
# it — a broken tag that IMMUTABLE forbids re-pushing. Builds therefore pass
# --provenance=false; see CLAUDE.md and the plan's Task 12.
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
