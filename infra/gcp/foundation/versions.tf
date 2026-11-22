terraform {
  # ci.yml's TERRAFORM_VERSION is the version CI runs; Renovate moves both.
  required_version = "~> 1.17"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.12.0"
    }
  }

  # The state lives in the bucket this configuration makes (state.tf), so its name is given
  # at init: `terraform init -backend-config=bucket=<project_id>-tfstate`. The first apply
  # runs before the bucket exists, with a local backend_override.tf (RUNBOOK.md, The GCP
  # foundation).
  backend "gcs" {
    prefix = "foundation"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # The budget API refuses user credentials without a project to bill the call to.
  billing_project       = var.project_id
  user_project_override = true

  default_labels = {
    managed-by = "terraform"
    stack      = "foundation"
  }
}
