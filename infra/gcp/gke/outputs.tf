# The cluster contract's create-step outputs, as modules/gke gives them, for install.
output "kube_context" {
  description = "The API endpoint and credentials, and the context install takes as CONTEXT."
  value       = module.gke.kube_context
}

output "api_server" {
  description = "The API server as the policy engine sees it."
  value       = module.gke.api_server
}

output "pod_range" {
  description = "The pods' range."
  value       = module.gke.pod_range
}

output "bucket" {
  description = "The backup bucket's URL, endpoint and credential mode."
  value       = module.gke.bucket
}

output "identities" {
  description = "The workload identity pool and the principals bound to the bucket and the secrets."
  value       = module.gke.identities
}

output "secret_store" {
  description = "The secret backend and where it is."
  value       = module.gke.secret_store
}

output "gateway_service" {
  description = "The front door Service's type and annotations."
  value       = module.gke.gateway_service
}

output "cluster_components" {
  description = "Components the cluster already has."
  value       = module.gke.cluster_components
}
