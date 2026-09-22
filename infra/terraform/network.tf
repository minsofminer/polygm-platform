# The two-plane network. The kit's requirement is that the executor has **no ingress** and an allowlisted egress;
# this file is where that is enforced at the platform level, and cloud-init/executor.yaml narrows it again at the
# host. Two layers because the platform firewall is per-server metadata and the host rules are what actually hold
# when a rule is edited in a hurry.

resource "hcloud_network" "polygm" {
  name     = "polygm"
  ip_range = "10.20.0.0/16"
}

resource "hcloud_network_subnet" "app" {
  network_id   = hcloud_network.polygm.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = "10.20.1.0/24"
}

locals {
  api_ip      = "10.20.1.10"
  executor_ip = "10.20.1.20"
}

# ---- the API plane: public ingress on 443 and 80 (TLS termination and the redirect), SSH from one place only.
resource "hcloud_firewall" "api" {
  name = "polygm-api"

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = var.admin_cidrs # NOT 0.0.0.0/0. The same rule P14's F14 holds Supabase to.
  }
  # Postgres is not reachable from the internet at all: the API host connects out to the managed database, and
  # nothing connects *in* to Postgres except the API and the executor over the private network.
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "5432"
    source_ips = ["${local.executor_ip}/32"]
  }
  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = ["10.20.0.0/16"]
  }
}

# ---- the executor plane: **no ingress rules at all**, and egress limited to the ports the job needs.
resource "hcloud_firewall" "executor" {
  name = "polygm-executor"

  # Outbound: DNS, the venue and the wallet provider over 443, the database over 5432, and the API host on the
  # port the wake-up call uses. Everything else is dropped by the absence of a rule.
  rule {
    direction       = "out"
    protocol        = "udp"
    port            = "53"
    destination_ips = ["1.1.1.1/32", "185.12.64.1/32", "185.12.64.2/32"] # Cloudflare + Hetzner resolvers, pinned
  }
  rule {
    direction       = "out"
    protocol        = "tcp"
    port            = "443"
    destination_ips = ["0.0.0.0/0"]
  }
  rule {
    direction       = "out"
    protocol        = "tcp"
    port            = "5432"
    destination_ips = ["0.0.0.0/0"] # the managed Postgres; the Neon project's host is pinned on the host side
  }
  rule {
    direction       = "out"
    protocol        = "tcp"
    port            = "8443"
    destination_ips = ["${local.api_ip}/32"]
  }
  rule {
    direction       = "out"
    protocol        = "tcp"
    port            = "22"
    destination_ips = ["10.20.0.0/16"] # the operator's break-glass path, over the private network
  }
}

resource "hcloud_ssh_key" "operator" {
  count      = length(var.ssh_public_keys)
  name       = "polygm-operator-${count.index}"
  public_key = var.ssh_public_keys[count.index]
}
