# tflint in CI (ci.yml, terraform): Terraform's recommended rules and Google's, without
# deep checks (they would call GCP). Renovate moves the plugin's version.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "google" {
  enabled = true
  version = "0.41.0"
  source  = "github.com/terraform-linters/tflint-ruleset-google"
}
