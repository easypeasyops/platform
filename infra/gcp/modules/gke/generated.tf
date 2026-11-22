# The values of the secrets a run makes itself (decided 2026-11-21: written at create), as
# kind's generators make them (deploy/secrets/generated.yaml): a random value per property,
# the secret's JSON. Kept in the run's state (the state bucket, read by CI and the laptop
# only) and gone with the run.
locals {
  generated = { for p in flatten([
    for id, props in var.generated_secrets : [for prop in props : { id = id, prop = prop }]
  ]) : "${p.id}/${p.prop}" => p }
}

resource "random_password" "generated" {
  for_each = local.generated

  length  = 24
  special = false
}

resource "google_secret_manager_secret_version" "generated" {
  for_each = var.generated_secrets

  secret = google_secret_manager_secret.this[each.key].id
  secret_data = jsonencode({
    for prop in each.value : prop => random_password.generated["${each.key}/${prop}"].result
  })
}
