output "api_ipv4" {
  value       = hcloud_server.api.ipv4_address
  description = "The origin behind Cloudflare; used by the deploy job's smoke step."
}

output "executor_ipv4_private" {
  value       = local.executor_ip
  description = "Private address only. There is no public name for this host, on purpose."
}

output "database_host" {
  value     = neon_project.polygm.database_host
  sensitive = true
}

output "backup_bucket" {
  value = cloudflare_r2_bucket.backups.name
}

output "monthly_cost_estimate_usd" {
  description = "What this topology is expected to cost per month, printed at apply time so the number is never a surprise."
  value       = "~$72 launch (~$19 Hetzner + ~$47 Neon + ~$5 Upstash + ~$1 R2); see docs/P15-deployment.md D2"
}
