# Two hosts, not one. The reason is the kit's isolation requirement: a process that can sign orders must not share
# a filesystem, a kernel and a network namespace with the process that serves the public internet.

resource "hcloud_server" "api" {
  name         = "polygm-api-1"
  server_type  = var.api_server_type
  image        = "ubuntu-24.04"
  location     = var.region
  ssh_keys     = hcloud_ssh_key.operator[*].id
  firewall_ids = [hcloud_firewall.api.id]
  labels       = { plane = "api", env = "prod" }

  network {
    network_id = hcloud_network.polygm.id
    ip         = local.api_ip
  }

  # `file()`, not `templatefile()`: the cloud-init scripts are shell, and a shell `${VAR}` is not a Terraform
  # interpolation. A template here would be a file that looks like infrastructure and fails at apply time.
  user_data = file("${path.module}/cloud-init/api.yaml")

  # The API host holds no key material: the signer lives with the executor (P07's envelope), so this box being
  # compromised costs sessions and caches rather than orders.
  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }
}

resource "hcloud_server" "executor" {
  name         = "polygm-executor-1"
  server_type  = var.executor_server_type
  image        = "ubuntu-24.04"
  location     = var.region
  ssh_keys     = hcloud_ssh_key.operator[*].id
  firewall_ids = [hcloud_firewall.executor.id]
  labels       = { plane = "executor", env = "prod" }

  network {
    network_id = hcloud_network.polygm.id
    ip         = local.executor_ip
  }

  # The executor gets a public IPv4 because it must reach the venue, but it accepts nothing inbound: the firewall
  # attached above has no `in` rules at all, and cloud-init narrows egress again by hostname.
  public_net {
    ipv4_enabled = true
    ipv6_enabled = false
  }

  user_data = file("${path.module}/cloud-init/executor.yaml")
}
