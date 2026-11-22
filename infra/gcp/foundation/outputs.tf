output "project_id" {
  description = "The project everything here lives in."
  value       = var.project_id
}

output "project_number" {
  description = "The project's number, which workload identity principals name (principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<project_id>.svc.id.goog/subject/ns/<ns>/sa/<ksa>)."
  value       = data.google_project.this.number
}

output "region" {
  description = "The region the GKE module builds in."
  value       = var.region
}

output "state_bucket" {
  description = "The state bucket: `-backend-config=bucket=` for this configuration and the GKE module's."
  value       = google_storage_bucket.state.name
}

output "workload_identity_provider" {
  description = "For google-github-actions/auth's workload_identity_provider (repository variable GCP_WORKLOAD_IDENTITY_PROVIDER)."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "ci_service_account" {
  description = "The account CI acts as (repository variable GCP_CI_SERVICE_ACCOUNT)."
  value       = google_service_account.ci.email
}

output "gke_node_service_account" {
  description = "The account the GKE module's nodes run as."
  value       = google_service_account.gke_nodes.email
}
