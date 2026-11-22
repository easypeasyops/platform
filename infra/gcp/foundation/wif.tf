# GitHub Actions authenticates as the CI service account (ci.tf) with its OIDC token, so no
# key is ever stored. The provider admits a token only from this repository (by ID, which a
# deleted and re-created repository of the same name doesn't keep), from main, and from
# one of the listed workflows: a pull request, another branch or a fork is refused here.
locals {
  ci_workflow_refs = [
    for workflow in var.ci_workflows :
    "${var.github_repository}/.github/workflows/${workflow}@refs/heads/main"
  ]
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  description               = "OIDC tokens from ${var.github_repository}'s workflows"

  depends_on = [google_project_service.this]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "platform"
  display_name                       = var.github_repository

  attribute_mapping = {
    "google.subject"          = "assertion.sub" # repo:<owner>/<repo>:ref:refs/heads/main
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.workflow_ref"  = "assertion.workflow_ref"
  }
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.workflow_ref in ${jsonencode(local.ci_workflow_refs)}",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}
