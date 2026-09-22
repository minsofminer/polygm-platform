# Postgres, managed. The arithmetic behind that choice (and the failure mode we are buying out of) is in
# docs/P15-deployment.md D2: even into 2026 the tape is ~45–90 GB per 90 days, which one managed instance holds
# comfortably, and the alternative is an on-call rota where "restore the database" is a procedure one person has
# ever run. Managed here does not mean open: `allowed_ips` is the same control P14's F14 asked Supabase for, and
# it is set at creation rather than remembered later.
#
# The **preview branch** is the reason Neon is here rather than a plain managed instance: a per-PR environment with
# the real schema and throwaway data, created and destroyed by CI.

resource "neon_project" "polygm" {
  name                      = "polygm-prod"
  region_id                 = "aws-eu-central-1" # the same continent as the Hetzner hosts: single-digit ms, not ~90
  pg_version                = 16
  history_retention_seconds = 7 * 24 * 60 * 60 # 7 days of PITR; the long tail is the nightly dump in R2

  # 1 CU is 1 vCPU / 4 GB. Autoscaling to 4 is what makes a news spike (10× tape) a bill line rather than an
  # outage; the ceiling is finite so a runaway query cannot quietly cost $400 in an hour.
  autoscaling_limit_min_cu = 1
  autoscaling_limit_max_cu = 4
  suspend_timeout_seconds  = 0 # production never suspends: a cold connection on the money path is an outage

  # Only these two hosts may open a connection. The operator's laptop is deliberately not on the list — admin work
  # goes through the Neon console or an ssh tunnel from the API host, which is auditable; a database reachable from
  # wherever the operator happens to be is the reason "restricted network" is a checkbox nobody can verify.
  allowed_ips = [
    "${hcloud_server.api.ipv4_address}/32",
    "${hcloud_server.executor.ipv4_address}/32",
  ]

  # The nightly dump is the recovery path that does not depend on the provider's own controls being right.
  default_branch_protected = true

  # Sunday 03:00 UTC: the quietest hour of the quietest day, and a maintenance window that is written down.
  maintenance_window {
    weekdays   = [7]
    start_time = "03:00"
    end_time   = "04:00"
  }

  branch {
    name          = "main"
    database_name = "polygm"
    role_name     = "polygm_app"
  }
}

# Preview: one branch per pull request, copied from a template that carries the schema and no user rows.
resource "neon_branch" "preview" {
  project_id = neon_project.polygm.id
  name       = "preview-template"
  parent_id  = neon_project.polygm.default_branch_id
}
