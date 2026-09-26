---
id: telegram-restricted
order: 11
severity: SEV2
owner: on-call (product)
alarms: [telegram-delivery-failing]
last_drilled: 2026-09-23
---

# Telegram bot restricted or banned

**One line:** the channel many users meet the product through stopped working — send failures, a webhook that
Telegram no longer calls, or a Mini App that will not open. Users are not at risk; their access is.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV2 `telegram-delivery-failing`: five or more delivery failures in 24 h.
* `alert_deliveries` rows with a failed state climbing, or `alert_fires` firing with no delivery;
* the webhook stops arriving, or the Mini App loads but `initData` validation fails for every user (a signature
  mismatch, not a session issue).

```bash
psql "$PGM_DB_URL" -c "SELECT state, count(*) FROM alert_deliveries
   WHERE created_ms > (extract(epoch from now())*1000 - 3600000)::bigint GROUP BY state;"
docker compose -f /srv/polygm/docker-compose.prod.yml logs --since 30m api | grep -iE "telegram|initdata|webhook" | tail -30
```

## Diagnosis

1. **Which failure:** sending (outbound), receiving (webhook), or authentication (Mini App signatures). The first
   two are the bot's problem; the third is often a *token* problem, which means P07's secret path.

```bash
curl -sS "https://api.telegram.org/bot$PGM_TELEGRAM_BOT_TOKEN/getWebhookInfo" | python3 -m json.tool
```

2. **A 403 from Telegram with a description naming a restriction is not our bug.** Read the description; it says
   whether it is a rate limit, a `chat not found`, or a bot-level restriction.

3. **Is the token the same one the Mini App signs with?** `initData` validation depends on the bot token
   (`HMAC_SHA256(key="WebAppData", msg=bot_token)`), so a rotated token invalidates every Mini App session until
   the API is redeployed with the new value. That is a one-line fix and a total outage if missed.

## Remediation

* **Webhook broken:** re-register it, with the current secret token, and confirm Telegram's own view:

```bash
curl -sS -X POST "https://api.telegram.org/bot$PGM_TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=$API/v1/telegram/webhook" -d "secret_token=$PGM_TELEGRAM_WEBHOOK_SECRET" | python3 -m json.tool
```

* **Bot restricted:** the owner appeals via BotFather/support; in the meantime, say so in the web app — the Mini
  App is a *convenience* channel, and the web terminal remains fully functional (this is the reason P12's bot and
  P08's web app share one money path).
* **Token rotated:** redeploy the API with `PGM_TELEGRAM_BOT_TOKEN` set, then verify `initData` validation by hand
  with a fresh link from the real app.

## Verification

* `getWebhookInfo` shows no `last_error_message`, and `last_error_date` is in the past.
* One alert fires and is *delivered*: check the row, not the intention.
* The Mini App opens from a phone and a trade can be placed (real-phone acceptance is an owner step; a desktop
  browser is not the test).

## Escalation

* Product owner within the hour. SEV2 because access is degraded, not money.
* If the restriction looks like a platform enforcement action, involve the owner immediately: the appeal has a
  deadline and the wording matters.
