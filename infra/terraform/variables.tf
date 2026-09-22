variable "hcloud_token" {
  type        = string
  sensitive   = true
  description = "Hetzner Cloud API token (project-scoped, read/write on this project only)."
}

variable "cloudflare_account_id" {
  type        = string
  description = "The Cloudflare account that owns the R2 bucket and the zone."
}

variable "cloudflare_api_token" {
  type        = string
  sensitive   = true
  description = "Cloudflare token: Zone.DNS edit on the zone, R2 admin for the bucket."
}

variable "neon_api_key" {
  type        = string
  sensitive   = true
  description = "Neon API key. Postgres is managed because a self-hosted database is a second job (see D2 failure modes)."
}

variable "domain" {
  type    = string
  default = "openout.app"
}

variable "ssh_public_keys" {
  type        = list(string)
  description = "Operator public keys. Exactly one human and the CI deploy key."
}

variable "admin_cidrs" {
  type        = list(string)
  description = "Where SSH may come from: the operator's address and the CI egress. Not 0.0.0.0/0 — P14's F14 is the same mistake on a different provider."
}

variable "region" {
  type    = string
  default = "fsn1" # Falkenstein: the cheap EU CPX line, and the closest region to Polymarket's edge
}

variable "api_server_type" {
  type    = string
  default = "cpx32" # 4 vCPU / 8 GB: the API, the ingest daemon and the caches live here at launch
}

variable "executor_server_type" {
  type    = string
  default = "cpx22" # 2 vCPU / 4 GB: one job, and it holds the signer's call path
}

