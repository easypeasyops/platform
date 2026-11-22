# A monthly budget on the project's spend, with email alerts to the billing account's
# admins at half, 90% and all of it, and when the month's forecast passes it. A budget
# alerts, it doesn't stop spending: the weekly run's teardown and reaper do (PT-120).
data "google_project" "this" {}

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "${var.project_id} monthly"

  # Spend before credits: while free-trial credit is left, spend after credits stays near
  # $0 and no alert would fire, even for a cluster a run failed to tear down.
  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "USD" # the billing account's currency, or the API refuses the budget
      units         = tostring(var.budget_usd)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  depends_on = [google_project_service.this]
}

# CI's plan reads the budget, which belongs to the billing account, not the project.
# Read-only, but it shows the whole billing account's costs.
resource "google_billing_account_iam_member" "ci_billing_viewer" {
  billing_account_id = var.billing_account
  role               = "roles/billing.viewer"
  member             = google_service_account.ci.member
}
