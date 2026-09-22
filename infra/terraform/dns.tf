# DNS and TLS. Cloudflare in front of the API for the same reason the Mini App is on Vercel: the certificate is
# somebody else's problem, and a DDoS is a page rather than a night.

data "cloudflare_zone" "polygm" {
  name = var.domain
}

resource "cloudflare_record" "api" {
  zone_id = data.cloudflare_zone.polygm.id
  name    = "api"
  type    = "A"
  content = hcloud_server.api.ipv4_address
  proxied = true # orange cloud: Cloudflare terminates TLS, the origin gets 443 from Cloudflare's ranges only
  ttl     = 1
}

resource "cloudflare_record" "executor" {
  # Deliberately NOT created. The executor has no public name, so nothing on the internet can resolve it and
  # nothing can be pointed at it by mistake. The absence is the design; this comment is the record of it.
  count   = 0
  zone_id = data.cloudflare_zone.polygm.id
  name    = "executor"
  type    = "A"
  content = hcloud_server.executor.ipv4_address
  proxied = false
  ttl     = 1
}

# TLS is automatic (Cloudflare universal certificates). The API's own config is what the P14 infra checks read:
# HSTS, frame-ancestors 'none', nosniff. Those live in `services/api/app.py` and are asserted there.

# The Mini App is on its own Vercel project (the owner's instruction) and its own name, so the BotFather URL is a
# name we control rather than an account-scoped vercel.app subdomain.
resource "cloudflare_record" "miniapp" {
  zone_id = data.cloudflare_zone.polygm.id
  name    = "trade"
  type    = "CNAME"
  content = "cname.vercel-dns.com"
  proxied = false # Vercel terminates TLS and needs to see the request directly to issue the certificate
  ttl     = 300
}
