output "project_id" {
  description = "The project everything here lives in."
  value       = var.project_id
}

output "region" {
  description = "The region the GKE module builds in."
  value       = var.region
}

output "state_bucket" {
  description = "The state bucket: `-backend-config=bucket=` for this configuration and the GKE module's."
  value       = google_storage_bucket.state.name
}
