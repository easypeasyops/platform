variable "project_id" {
  description = "The project the run builds in (the foundation's project_id)."
  type        = string
}

variable "project_number" {
  description = "That project's number, which workload identity principals name (the foundation's project_number)."
  type        = string
}

variable "region" {
  description = "The region of the run's subnet, NAT and bucket (the foundation's region)."
  type        = string
}

variable "zone" {
  description = "The zone of the zonal cluster and its node pool, in region."
  type        = string
}

variable "run_id" {
  description = "Names and labels everything the run makes (label run-id), so the reaper (PT-120) finds what a failed run left."
  type        = string

  validation {
    # The cluster's name is platform-<run_id>, at most 40 characters; a label value allows these.
    condition     = can(regex("^[a-z][a-z0-9-]{0,29}[a-z0-9]$", var.run_id))
    error_message = "A run ID is 2 to 31 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "node_service_account" {
  description = "The account the nodes run as (the foundation's gke_node_service_account)."
  type        = string
}

variable "machine_type" {
  description = "The one Spot node's type. e2-standard-4 (4 vCPU, 16 GB): the platform's ~5.3 GiB and 905m budgets, the apps and GKE's own pods (DESIGN.md, The GKE run)."
  type        = string
  default     = "e2-standard-4"
}

variable "min_master_version" {
  description = "The cluster contract's Kubernetes floor: create fails if the release channel has nothing at or above it."
  type        = string
  default     = "1.37"
}

variable "pod_range" {
  description = "The pods' secondary range: kind's 10.244.0.0/16, which an app's values trust as its proxy (cluster contract, nodes)."
  type        = string
  default     = "10.244.0.0/16"
}
