# The APIs the foundation and the GKE module (PT-119) call. A destroy leaves them enabled:
# disabling one breaks whatever still uses it, and an enabled API costs nothing.
locals {
  services = [
    "cloudresourcemanager.googleapis.com", # project IAM
    "serviceusage.googleapis.com",         # this list
    "storage.googleapis.com",              # the state bucket
  ]
}

resource "google_project_service" "this" {
  for_each = toset(local.services)

  service            = each.value
  disable_on_destroy = false
}
