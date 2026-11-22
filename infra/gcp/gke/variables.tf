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

variable "backup_principals" {
  description = "Who uses the backup bucket (modules/gke): the restore drills' namespace, and each app database the run deploys (namespace <app>-<env>, service account its Cluster's name)."
  type = list(object({
    namespace       = string
    service_account = optional(string)
  }))
  default = [{ namespace = "restore-drill" }]
}

variable "secret_stores" {
  description = "Each ClusterSecretStore's service account and the secrets it reads (modules/gke): the `platform` store's, by its remote keys in deploy/secrets/."
  type        = map(list(string))
  default = {
    store-reader = ["grafana-admin"]
  }
}

variable "generated_secrets" {
  description = "The secrets whose values the run makes (modules/gke), as deploy/secrets/generated.yaml does on kind: Grafana's admin password. Slack's bot token isn't one: a GKE run installs with SLACK=0."
  type        = map(list(string))
  default = {
    grafana-admin = ["admin-password"]
  }
}
