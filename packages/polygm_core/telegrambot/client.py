"""The only module in this product that speaks to Telegram.

Eight methods, one token, and three rules that are about failure rather than about success:

* **The token is never logged, never echoed, and never in an error message.** A Bot API error body echoes the URL
  it was called on, and the URL carries the token — which is how a bot token ends up in a log aggregator, where it
  is worth more than the account it protects (it can read every message the bot can read). `_redact()` runs over
  every string that leaves this module, and the test asserts the token does not survive it.
* **A send is a *decision*, not an attempt.** The methods return `SendResult`, whose `status=0` means "we do not
  know whether it arrived" — the one outcome that must never be silently treated as a failure, because a fill
  notification re-sent twice is a user who thinks they bought twice.
* **Webhook mode only in production.** `set_webhook` is called with the secret token that Telegram then echoes on
  every request (`X-Telegram-Bot-Api-Secret-Token`); the receiver compares it in constant time. Long-polling is
  supported for local development via `getUpdates`, because a webhook needs a public URL and a laptop does not have
  one — but the two paths share this client, so a message built in development is the message built in production.

⚠️ Two things to re-verify before launch rather than trust from this file: the current rate limits (they are the
numbers in `outbox.py`) and whether `protect_content` still hides forwarded messages as expected.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

API = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{4,12}:[A-Za-z0-9_-]{30,}$")

#: Methods this product uses. A closed set, so a typo is an import-time error rather than a 404 from Telegram.
METHODS = ("sendMessage", "editMessageText", "editMessageReplyMarkup", "answerCallbackQuery", "sendChatAction",
           "sendPhoto", "deleteMessage", "setWebhook", "deleteWebhook", "getMe")


class BotError(RuntimeError):
    """A refusal from the Bot API, with the description Telegram gave (redacted) and the HTTP status."""

    def __init__(self, status: int, description: str, *, retry_after_s: int = 0):
        super().__init__("%d %s" % (status, description))
        self.status = status
        self.description = description
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class SendResult:
    ok: bool
    status: int = 0
    message_id: int = 0
    retry_after_s: int = 0
    note: str = ""
    raw: dict = field(default_factory=dict)


def token_from_env() -> str:
    """The bot token, validated for *shape* before it is used.

    A malformed token is a misconfiguration, and the difference between 401 on every call and a clear message at
    boot is an hour. The shape is Telegram's: `<bot id>:<35-ish chars>`; `security/telegram.py` has the same regex
    for `initData`, and the two must agree.
    """
    tok = (os.environ.get("PGM_TELEGRAM_BOT_TOKEN") or "").strip()
    if not tok:
        raise BotError(503, "no bot token configured (PGM_TELEGRAM_BOT_TOKEN)")
    if not TOKEN_RE.match(tok):
        raise BotError(503, "the configured bot token is not shaped like one")
    return tok


def _redact(text: str, token: str) -> str:
    """Remove the token from anything that might be logged: the URL, the path, or an echoed body."""
    out = str(text)
    if token:
        out = out.replace(token, "<bot-token>")
    return re.sub(r"\d{4,12}:[A-Za-z0-9_-]{30,}", "<bot-token>", out)


class BotClient:
    """A minimal Bot API client. `token=""` means "read it from the environment on first use"."""

    def __init__(self, token: str = "", *, base: str = API, timeout_s: float = 10.0, opener=None,
                 calls: list | None = None):
        self._token = token
        self.base = base
        self.timeout_s = timeout_s
        #: Injected in tests: the client is the seam, so no test ever opens a socket to Telegram.
        self._opener = opener
        self.calls = calls if calls is not None else []

    # ------------------------------------------------------------------ plumbing
    @property
    def token(self) -> str:
        if not self._token:
            self._token = token_from_env()
        return self._token

    def _url(self, method: str) -> str:
        if method not in METHODS:
            raise BotError(400, "unknown method %r" % method)
        return "%s/bot%s/%s" % (self.base.rstrip("/"), self.token, method)

    def call(self, method: str, payload: dict, *, timeout_s: float | None = None) -> SendResult:
        """One API call, recorded for the tests and for the audit trail.

        The record keeps the method and a *redacted* URL: `calls` is what a test asserts on and what an operator
        reads when a message did not arrive, so it must be safe to print.
        """
        url = self._url(method)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        record = {"method": method, "url": _redact(url, self.token), "bytes": len(body),
                  "chat_id": str(payload.get("chat_id") or ""), "at_ms": int(time.time() * 1000)}
        self.calls.append(record)
        if self._opener is None:
            return SendResult(ok=False, status=0, note="no transport configured (dev/test client)")
        try:
            status, text = self._opener(url, body, timeout_s or self.timeout_s)
        except Exception as exc:                                   # a network failure we cannot classify
            record["error"] = _redact(str(exc), self.token)[:200]
            return SendResult(ok=False, status=0, note="transport: " + _redact(str(exc), self.token)[:120])
        try:
            data = json.loads(text)
        except ValueError:
            return SendResult(ok=False, status=status, note="the API answered with something that is not JSON")
        if status == 429 or (isinstance(data, dict) and data.get("parameters", {}).get("retry_after")):
            retry = int((data.get("parameters") or {}).get("retry_after") or 0)
            return SendResult(ok=False, status=429, retry_after_s=retry,
                              note=_redact(str(data.get("description") or "rate limited"), self.token)[:160])
        if not isinstance(data, dict) or not data.get("ok"):
            desc = _redact(str((data or {}).get("description") or ""), self.token)[:200]
            record["error"] = desc
            return SendResult(ok=False, status=status, note=desc or "the API refused the call")
        result = data.get("result") or {}
        return SendResult(ok=True, status=status, message_id=int(result.get("message_id") or 0), raw=result)

    # ------------------------------------------------------------------ messages
    def send_message(self, *, chat_id: str, text: str, keyboard: dict | None = None, disable_preview: bool = True,
                     protect: bool = True) -> SendResult:
        """Send, in HTML, with no link preview unless we asked for one.

        `protect_content=True` is the default because our messages carry a user's positions: a forward of a
        position card is a leak the user did not intend, and Telegram will hide the forward button for us.
        """
        payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": bool(disable_preview)},
                   "protect_content": bool(protect)}
        if keyboard:
            payload["reply_markup"] = keyboard
        return self.call("sendMessage", payload)

    def edit_message(self, *, chat_id: str, message_id: int, text: str, keyboard: dict | None = None,
                     keep_keyboard: bool = False) -> SendResult:
        """Edit in place — the two-beat's second half, and the reason we do not send a second message.

        `keep_keyboard` exists because `editMessageText` *removes* the keyboard when `reply_markup` is omitted:
        the card would lose its buttons the moment its text was updated, which is the kind of thing that only shows
        up in production.
        """
        payload = {"chat_id": str(chat_id), "message_id": int(message_id), "text": text, "parse_mode": "HTML"}
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        elif keep_keyboard:
            payload["reply_markup"] = {"inline_keyboard": []}
        return self.call("editMessageText", payload)

    def edit_keyboard(self, *, chat_id: str, message_id: int, keyboard: dict) -> SendResult:
        """Swap the buttons under a message in place — the confirmation animation, and the dead-tap defence."""
        return self.call("editMessageReplyMarkup", {"chat_id": str(chat_id), "message_id": int(message_id),
                                                    "reply_markup": keyboard})

    def answer_callback(self, *, callback_id: str, text: str = "", alert: bool = False, url: str = "") -> SendResult:
        """Stop the client's spinner. Always called first, because the spin has a deadline."""
        payload = {"callback_query_id": str(callback_id), "cache_time": 0}
        if text:
            payload["text"] = text[:200]
        if alert:
            payload["show_alert"] = True
        if url:
            payload["url"] = url
        return self.call("answerCallbackQuery", payload)

    def chat_action(self, *, chat_id: str, action: str = "typing") -> SendResult:
        return self.call("sendChatAction", {"chat_id": str(chat_id), "action": action})

    # ------------------------------------------------------------------ webhook
    def set_webhook(self, *, url: str, secret: str, drop_pending: bool = True,
                    allowed: tuple = ("message", "callback_query", "channel_post")) -> SendResult:
        """Webhook mode, with the secret Telegram echoes back on every request.

        `drop_pending_updates=True` on a redeploy is deliberate and it is a trade: updates that arrived while the
        old process was dying are usually replays of a button tap the user already saw answered, and reprocessing
        them makes the bot answer an old tap twice. The dedupe table is the backstop either way.
        """
        return self.call("setWebhook", {"url": url, "secret_token": secret,
                                        "drop_pending_updates": bool(drop_pending),
                                        "allowed_updates": list(allowed),
                                        "max_connections": 4})

    def delete_webhook(self, *, drop_pending: bool = False) -> SendResult:
        return self.call("deleteWebhook", {"drop_pending_updates": bool(drop_pending)})


def webhook_findings(request_headers: dict, expected_secret: str) -> list:
    """The webhook's own checks, as a pure function — the shape the gate canaries.

    Two different refusals, and the difference matters: an *unset* secret is a misconfiguration (503, closed, and
    the on-call is told), while a *wrong* secret is either a misdeploy or someone probing for an open endpoint
    (403). Nothing about the update is processed before this passes, because a forged update that reaches the
    router is a forged trade.
    """
    out = []
    if not expected_secret:
        out.append("no webhook secret is configured, so this endpoint cannot tell Telegram from anybody else")
    got = ""
    for key, value in (request_headers or {}).items():
        if str(key).lower() == "x-telegram-bot-api-secret-token":
            got = str(value)
    if not got:
        out.append("the request carries no X-Telegram-Bot-Api-Secret-Token header")
    elif expected_secret and got != expected_secret:
        out.append("the secret token does not match")
    return out
