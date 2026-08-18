# budget.tf
# A backstop, not the primary control. AWS billing data lags 8-24 hours,
# so this catches "forgot for days" and never "forgot overnight." The real
# check is `aws eks list-clusters` at the end of every session.
#
# FORECASTED rather than ACTUAL: it fires when AWS projects an overrun,
# which is earlier than waiting for spend to land.

resource "aws_budgets_budget" "monthly" {
  name         = "platform-lab-monthly"
  budget_type  = "COST"
  limit_amount = "20"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
