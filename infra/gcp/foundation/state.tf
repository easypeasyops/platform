# Terraform's state, for the foundation (prefix foundation/) and the GKE module. Versioned,
# so a bad write can be rolled back to an earlier generation.
resource "google_storage_bucket" "state" {
  name     = "${var.project_id}-tfstate"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Old generations: the last 10 of each object are enough to roll back.
  lifecycle_rule {
    condition {
      num_newer_versions = 10
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.this]
}
