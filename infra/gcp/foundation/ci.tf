# The identity CI acts as: it builds, tests and destroys the weekly GKE run (PT-119, PT-120)
# and plans this configuration on main. It can't change IAM on the project or on any
# service account, so it can't widen its own access: this configuration is applied from
# the laptop (RUNBOOK.md, The GCP foundation).
resource "google_service_account" "ci" {
  account_id   = "platform-ci"
  display_name = "Platform CI"
  description  = "GitHub Actions in ${var.github_repository}, through Workload Identity Federation"
}

# Whatever the provider admits (wif.tf) may act as it, from this repository only.
resource "google_service_account_iam_member" "ci_github" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

locals {
  # Each role, and what of the run needs it. A pod's access (backups, secrets) is granted to
  # its Kubernetes service account directly (GKE's workload identity principal), on the
  # bucket or the secret, so CI makes no service accounts.
  ci_roles = {
    "roles/container.admin"                   = "create and delete the zonal cluster and its Spot node pool; cluster-admin inside it for cluster.sh install"
    "roles/compute.networkAdmin"              = "the run's VPC, subnet, router and NAT; delete load balancers a Service left behind"
    "roles/compute.securityAdmin"             = "delete the firewall rules GKE writes per LoadBalancer Service, which otherwise block deleting the VPC"
    "roles/compute.storageAdmin"              = "delete disks a PersistentVolume left behind"
    "roles/secretmanager.admin"               = "the run's secrets, and the IAM on each that lets External Secrets read it"
    "roles/iam.securityReviewer"              = "plan: read the IAM policies this configuration sets"
    "roles/iam.serviceAccountViewer"          = "plan: read the service accounts"
    "roles/iam.workloadIdentityPoolViewer"    = "plan: read the pool and provider"
    "roles/serviceusage.serviceUsageViewer"   = "plan: read the enabled APIs"
    "roles/serviceusage.serviceUsageConsumer" = "bill its API calls to this project (the provider's user_project_override)"
  }
}

resource "google_project_iam_member" "ci" {
  for_each = local.ci_roles

  project = var.project_id
  role    = each.key
  member  = google_service_account.ci.member
}

# Buckets: the run's backup bucket, and its IAM for the backup pods. Every bucket but the
# state bucket, whose objects (the state, its lock) CI reads and writes, but which it can't
# delete or reconfigure.
resource "google_project_iam_member" "ci_buckets" {
  project = var.project_id
  role    = "roles/storage.admin"
  member  = google_service_account.ci.member

  condition {
    title      = "not-the-state-bucket"
    expression = "!(resource.name == 'projects/_/buckets/${google_storage_bucket.state.name}' || resource.name.startsWith('projects/_/buckets/${google_storage_bucket.state.name}/'))"
  }
}

resource "google_storage_bucket_iam_member" "ci_state" {
  for_each = toset(["roles/storage.objectUser", "roles/storage.legacyBucketReader"])

  bucket = google_storage_bucket.state.name
  role   = each.value
  member = google_service_account.ci.member
}

# The nodes run as their own account with GKE's minimal node role, not Compute Engine's
# default account (Editor on the project). CI may only attach it to the nodes it creates.
resource "google_service_account" "gke_nodes" {
  account_id   = "gke-nodes"
  display_name = "GKE nodes"
  description  = "The weekly GKE run's nodes (PT-119)"
}

resource "google_project_iam_member" "gke_nodes" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = google_service_account.gke_nodes.member
}

resource "google_service_account_iam_member" "ci_uses_gke_nodes" {
  service_account_id = google_service_account.gke_nodes.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.ci.member
}
