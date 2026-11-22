# What pods reach outside the cluster, granted to their Kubernetes service accounts
# directly (GKE's workload identity principals): no Google service account, no key.
locals {
  principal_prefix = "iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/${local.pool}"
  backup_members = [
    for p in var.backup_principals : p.service_account == null
    ? "principalSet://${local.principal_prefix}/namespace/${p.namespace}"
    : "principal://${local.principal_prefix}/subject/ns/${p.namespace}/sa/${p.service_account}"
  ]
  store_members = {
    for sa, _ in var.secret_stores : sa => "principal://${local.principal_prefix}/subject/ns/secret-store/sa/${sa}"
  }
  store_secrets = merge([
    for sa, secrets in var.secret_stores : { for s in secrets : s => sa }
  ]...)
}

# The run's backups (cluster contract, bucket): Barman Cloud writes <bucket>/<namespace>.
# Deleted with the run, objects and all.
resource "google_storage_bucket" "backups" {
  name                        = "${var.project_id}-backups-${var.run_id}"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  soft_delete_policy {
    retention_duration_seconds = 0 # nothing billed for a week after the run deletes it
  }
}

resource "google_storage_bucket_iam_member" "backups" {
  for_each = toset(local.backup_members)

  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectUser"
  member = each.value

  depends_on = [google_container_cluster.this] # the pool exists once a cluster uses it
}

# The run's secrets: made empty here, each readable by its store's service account only.
# Named as the ExternalSecrets' remote keys are (grafana-admin), so no consumer changes; a
# failed run's leftover makes the next apply fail until the reaper (PT-120) removes it.
resource "google_secret_manager_secret" "this" {
  for_each = local.store_secrets

  secret_id = each.key
  labels    = local.labels

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_iam_member" "store" {
  for_each = local.store_secrets

  secret_id = google_secret_manager_secret.this[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.store_members[each.value]

  depends_on = [google_container_cluster.this]
}
