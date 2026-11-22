variable "project_id" {
  description = "The GCP project the platform's cloud runs use, created by hand with billing linked (RUNBOOK.md, The GCP foundation)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "A project ID is 6 to 30 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "billing_account" {
  description = "The billing account the project is linked to (XXXXXX-XXXXXX-XXXXXX): the budget lives on it."
  type        = string

  validation {
    condition     = can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account))
    error_message = "A billing account ID looks like 0X0X0X-0X0X0X-0X0X0X."
  }
}

variable "budget_usd" {
  description = "The project's monthly budget in US dollars: alerts at 50%, 90% and 100%, and on a forecast above it."
  type        = number
  default     = 10
}

variable "region" {
  description = "Where the state bucket lives, and the region the GKE module builds in."
  type        = string
  default     = "us-west1"
}

variable "github_repository" {
  description = "The repository whose workflows may act as the CI service account (owner/name)."
  type        = string
  default     = "easypeasyops/platform"
}

variable "github_repository_id" {
  description = "That repository's numeric ID (gh api repos/<owner>/<name> --jq .id): a re-created repository of the same name gets a new one."
  type        = string
  default     = "1436120298"
}

variable "ci_workflows" {
  description = "The workflow files under .github/workflows/ that may act as the CI service account, from main. A change is applied from the laptop before the workflow first runs (RUNBOOK.md, The GCP foundation)."
  type        = list(string)
  default     = ["terraform-plan.yml", "gke-e2e.yml", "gke-reaper.yml"]
}
