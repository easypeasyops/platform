# Exactly what a create step hands to install (DESIGN.md, Cluster contract, "What create
# hands to install"): one output per item, named as there.

output "kube_context" {
  description = "The API endpoint and credentials: get_credentials writes the context `name`, which install takes as CONTEXT."
  value = {
    name            = "gke_${var.project_id}_${var.zone}_${google_container_cluster.this.name}"
    endpoint        = "https://${google_container_cluster.this.endpoint}"
    ca_certificate  = google_container_cluster.this.master_auth[0].cluster_ca_certificate
    get_credentials = "gcloud container clusters get-credentials ${google_container_cluster.this.name} --zone ${var.zone} --project ${var.project_id}"
  }
}

output "api_server" {
  description = "The API server as the policy engine sees it, for networkPolicy.apiServerTo and apiServerPort: the control plane's private endpoint on 443. To be confirmed on the first run (cluster contract, network policy)."
  value = {
    cidrs = ["${google_container_cluster.this.private_cluster_config[0].private_endpoint}/32"]
    port  = 443
  }
}

output "pod_range" {
  description = "The pods' range, which an app may trust as its proxy."
  value       = var.pod_range
}

output "bucket" {
  description = "The backup bucket: its URL, endpoint (none for GCS) and credential mode (the service chart's database.cluster.backup)."
  value = {
    url         = "gs://${google_storage_bucket.backups.name}"
    endpoint    = null
    credentials = "workloadIdentity"
  }
}

output "identities" {
  description = "The workload identity pool, and the principal each ClusterSecretStore's service account and the backups act as: already bound here, nothing to annotate."
  value = {
    pool          = local.pool
    secret_stores = local.store_members
    backups       = local.backup_members
  }
}

output "secret_store" {
  description = "The secret backend External Secrets' stores use: Secret Manager in this project, through workload identity on this cluster."
  value = {
    provider         = "gcpsm"
    project_id       = var.project_id
    cluster_name     = google_container_cluster.this.name
    cluster_location = var.zone
  }
}

output "gateway_service" {
  description = "The front door's Service: a LoadBalancer on a Google Cloud load balancer, no annotations."
  value = {
    type        = "LoadBalancer"
    annotations = {}
  }
}

output "cluster_components" {
  description = "Components the cluster already has, which the platform must not install."
  value       = ["metrics-server"]
}
