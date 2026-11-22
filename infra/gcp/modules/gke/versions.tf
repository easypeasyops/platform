# The GKE run's cluster and what it reaches outside it (PT-119). Called by infra/gcp/gke/,
# whose lock file pins the provider.
terraform {
  required_version = "~> 1.17"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.12"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}
