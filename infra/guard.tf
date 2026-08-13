# Fails at plan time if the configured credentials point at a different AWS
# account than intended.
#
# This account is shared with other projects, so a wrong AWS_PROFILE does not
# produce an error — it produces a second, parallel InboxPilot stack somewhere
# unexpected, billed silently, and a state file that now disagrees with reality.
# Cheap insurance against an expensive afternoon.
#
# Set allowed_account_id = "" to disable, e.g. when deliberately standing the
# stack up in a new account.

resource "terraform_data" "account_guard" {
  input = data.aws_caller_identity.current.account_id

  lifecycle {
    precondition {
      condition = (
        var.allowed_account_id == "" ||
        data.aws_caller_identity.current.account_id == var.allowed_account_id
      )
      error_message = "Wrong AWS account. Credentials resolve to ${data.aws_caller_identity.current.account_id}, but allowed_account_id is ${var.allowed_account_id}. Check AWS_PROFILE, or set allowed_account_id to \"\" if this is deliberate."
    }
  }
}
