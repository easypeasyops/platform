variable "project_id" {
  description = "The GCP project the platform's cloud runs use, created by hand with billing linked (RUNBOOK.md, The GCP foundation)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "A project ID is 6 to 30 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "region" {
  description = "Where the state bucket lives, and the region the GKE module builds in."
  type        = string
  default     = "us-west1"
}
