locals {
  name   = "platform-${var.run_id}"
  labels = { run-id = var.run_id }
  pool   = "${var.project_id}.svc.id.goog" # GKE's workload identity pool
}

# GKE Standard, zonal: Autopilot refuses the privileged pods node-exporter and the log
# collector need, and one zonal cluster's management fee is within GKE's free tier.
resource "google_container_cluster" "this" {
  name     = local.name
  location = var.zone

  network         = google_compute_network.this.id
  subnetwork      = google_compute_subnetwork.this.id
  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Dataplane V2 (Cilium) enforces NetworkPolicy, the boundary for the databases, Redis and
  # the drills (cluster contract, network policy), without Calico's extra pods on the node.
  datapath_provider = "ADVANCED_DATAPATH"

  release_channel {
    channel = "REGULAR"
  }
  min_master_version = var.min_master_version

  workload_identity_config {
    workload_pool = local.pool
  }

  # Private nodes; the endpoint stays public, behind IAM, as GitHub's runners have no fixed
  # address.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  addons_config {
    # LoadBalancer Services need only the cloud controller; this addon is GKE's Ingress.
    http_load_balancing {
      disabled = true
    }
  }
  # Envoy Gateway ships and owns the Gateway API CRDs: GKE's must stay off.
  gateway_api_config {
    channel = "CHANNEL_DISABLED"
  }

  # Google's logging and monitoring of the system only: the platform has its own.
  logging_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = false
    }
  }

  resource_labels = local.labels

  # No default node pool (no initial_node_count or node_config): only the one below.
  deletion_protection = false # made to be destroyed at the end of the run
}

resource "google_container_node_pool" "spot" {
  name       = "spot"
  cluster    = google_container_cluster.this.id
  location   = var.zone
  node_count = 1 # one node, like kind's: one architecture (cluster contract, nodes)

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.machine_type
    spot            = true
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    image_type      = "COS_CONTAINERD"
    service_account = var.node_service_account
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"] # IAM decides
    tags            = [local.name]
    resource_labels = local.labels

    workload_metadata_config {
      mode = "GKE_METADATA" # pods get their Kubernetes service account's identity, not the node's
    }
    shielded_instance_config {
      enable_secure_boot = true
    }
  }
}
