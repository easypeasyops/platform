# The run's own network: a VPC per run, so deleting it proves nothing else was left on it.
# Private nodes, egress through Cloud NAT (cluster contract, egress). A VPC, subnet, router
# and firewall take no labels: their name carries the run ID.
resource "google_compute_network" "this" {
  name                    = local.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "this" {
  name                     = local.name
  network                  = google_compute_network.this.id
  region                   = var.region
  ip_cidr_range            = "10.10.0.0/24" # the nodes
  private_ip_google_access = true           # GCS and Secret Manager without NAT

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pod_range
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.96.0.0/20"
  }
}

resource "google_compute_router" "this" {
  name    = local.name
  network = google_compute_network.this.id
  region  = var.region
}

resource "google_compute_router_nat" "this" {
  name                               = local.name
  router                             = google_compute_router.this.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# GKE lets the control plane reach private nodes on 443 and 10250 only. The platform's
# admission webhooks listen elsewhere (Kyverno's and CloudNativePG's on 9443), and a
# webhook the API server can't reach refuses every request it judges.
resource "google_compute_firewall" "control_plane_webhooks" {
  name          = "${local.name}-webhooks"
  network       = google_compute_network.this.id
  direction     = "INGRESS"
  source_ranges = ["${google_container_cluster.this.private_cluster_config[0].private_endpoint}/32"]
  target_tags   = [local.name]

  allow {
    protocol = "tcp"
    ports    = ["8443", "9443"]
  }
}
