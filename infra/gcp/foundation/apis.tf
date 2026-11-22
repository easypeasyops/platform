# The APIs the foundation and the GKE module (PT-119) call. A destroy leaves them enabled:
# disabling one breaks whatever still uses it, and an enabled API costs nothing.
locals {
  services = [
    "cloudresourcemanager.googleapis.com", # project IAM
    "serviceusage.googleapis.com",         # this list
    "storage.googleapis.com",              # the state bucket, the run's backup bucket
    "iam.googleapis.com",                  # service accounts, the identity pool
    "iamcredentials.googleapis.com",       # CI's short-lived tokens for its service account
    "sts.googleapis.com",                  # GitHub's OIDC token exchanged for a Google one
    "compute.googleapis.com",              # the run's network, disks and load balancers
    "container.googleapis.com",            # GKE
    "secretmanager.googleapis.com",        # the run's secrets, read by External Secrets
    "billingbudgets.googleapis.com",       # the budget
    "cloudbilling.googleapis.com",         # the billing account's IAM
  ]
}

resource "google_project_service" "this" {
  for_each = toset(local.services)

  service            = each.value
  disable_on_destroy = false
}
