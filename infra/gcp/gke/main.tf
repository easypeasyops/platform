# One GKE run (PT-119): the cluster modules/gke makes, in the foundation's project, as CI's
# service account. Its state is the run's own, beside the foundation's:
#   terraform init -backend-config=bucket=<project_id>-tfstate -backend-config=prefix=gke/<run_id>
# The weekly workflow (PT-120) applies it, installs, tests and destroys it.
terraform {
  required_version = "~> 1.17"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.12.0"
    }
  }

  backend "gcs" {}
}

# The foundation's outputs: the project, its number, the region, the nodes' account.
data "terraform_remote_state" "foundation" {
  backend = "gcs"
  config = {
    bucket = var.state_bucket
    prefix = "foundation"
  }
}

locals {
  foundation = data.terraform_remote_state.foundation.outputs
}

provider "google" {
  project = local.foundation.project_id
  region  = local.foundation.region

  default_labels = {
    managed-by = "terraform"
    stack      = "gke"
    run-id     = var.run_id
  }
}

module "gke" {
  source = "../modules/gke"

  project_id           = local.foundation.project_id
  project_number       = local.foundation.project_number
  region               = local.foundation.region
  zone                 = "${local.foundation.region}-${var.zone_suffix}"
  run_id               = var.run_id
  node_service_account = local.foundation.gke_node_service_account
  backup_principals    = var.backup_principals
  secret_stores        = var.secret_stores
}
