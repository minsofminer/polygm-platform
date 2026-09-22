# P15 D2 — the whole topology, in one place. No console-clicked infrastructure: the kit's constraint, and the
# reason this directory exists rather than a runbook of things to click.
terraform {
  required_version = ">= 1.6"

  # State lives remotely even for a single engineer. A laptop-held state file is how two machines disagree about
  # what exists — and the day that matters is the day something is on fire.
  backend "s3" {
    bucket                      = "polygm-terraform-state"
    key                         = "polygm/terraform.tfstate"
    region                      = "auto"
    endpoints                   = { s3 = "https://<accountid>.r2.cloudflarestorage.com" }
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
  }

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
    neon = {
      source  = "kislerdm/neon"
      version = "~> 0.6"
    }
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

provider "neon" {
  api_key = var.neon_api_key
}
