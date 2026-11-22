variable "state_bucket" {
  description = "The foundation's state bucket (<project_id>-tfstate), where this reads its outputs."
  type        = string
}

variable "run_id" {
  description = "The run's ID: names and labels everything it makes (modules/gke)."
  type        = string
}

variable "zone_suffix" {
  description = "The cluster's zone in the foundation's region."
  type        = string
  default     = "b"
}
