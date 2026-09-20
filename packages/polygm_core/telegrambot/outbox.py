"""The outbound queue: Telegram's rate limits, our priorities, and the arithmetic that keeps both true.

Telegram's published limits (⚠️ re-verify at launch, they are the ones that change without notice): about **30
messages a second** across all chats for one bot, about **1 message a second in a single chat**, and about **20
messages a minute to a group**. Exceeding them is not a 429 we can retry our way out of: the API starts *dropping*
messages and, for a channel, restricting the bot — and the messages it drops are the ones that were queued behind
the burst.

That is why this is a queue with three buckets rather than a `sleep` between sends:

* **the global bucket** — 30/s, in one-second windows;
* **the per-chat bucket** — 1/s for private chats, 20/min (with a 3 s floor between messages) for groups and
  channels, because the group limit is a *minute* limit and a per-second rule would burst straight past it;
* **the priority order** — a fill notification beats a personal alert, which beats a broadcast. The kit's sentence
  is exact ("priority so a paying user's fill notification is not stuck behind the free channel's broadcast"), and
  it is the difference between a product and a firehose: the broadcast is the thing we generate in bulk, and it is
  the thing that must yield.

The queue is a table (`telegram_outbox`) so a restart does not lose a fill notification, and `plan()` is a pure
function of (queue, buckets, now) so the gate can prove the ordering without a bot token. `claim()` is the
transactional half: it marks rows as sent so two workers cannot send the same alert twice, which — for a fill
notification — is a duplicate the user cannot tell from a real second fill.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SECOND = 1_000
MINUTE = 60 * SECOND

#: The kit's numbers, in one place, with the re-verify note attached to the constant rather than to a doc.
GLOBAL_PER_SECOND = 30
CHAT_PER_SECOND = 1
GROUP_PER_MINUTE = 20
GROUP_MIN_GAP_MS = 3_000

#: Priorities: lower sorts first. The gaps are wide so a new priority can be inserted without renumbering, and
#: named constants are used everywhere instead of the numbers.
P_FILL = 10            # an order of YOURS filled: the message the product exists to deliver
P_REJECT = 20          # your order was rejected, in plain language
P_TRADE_CARD = 30      # the confirm card you are waiting on
P_ALERT_PERSONAL = 40  # your own rule fired
P_COMMAND_REPLY = 50   # the answer to something you typed
P_ALERT_CHANNEL = 70   # the public channel's broadcast: the acquisition engine, and the thing that yields
P_BULK = 90            # announcements, digests, onboarding drips

PRIORITY_NAMES = {P_FILL: "fill", P_REJECT: "reject", P_TRADE_CARD: "trade_card", P_ALERT_PERSONAL: "alert",
                  P_COMMAND_REPLY: "reply", P_ALERT_CHANNEL: "channel", P_BULK: "bulk"}

#: `parse_mode=HTML` is the only mode we use: MarkdownV2 escapes every `.`, `-` and `!` in a market question and
#: produces the backslash confetti that makes a bot look broken. HTML needs three characters escaped and nothing
#: else, and `render.py` escapes them at the only place text is assembled.
MAX_TEXT = 4_096        # Telegram's hard cap for a message; longer text is split, never truncated mid-sentence
MAX_CAPTION = 1_024


@dataclass
class Bucket:
    """A token bucket with a window, in integer milliseconds."""

    capacity: int
    window_ms: int
    used: int = 0
    window_start_ms: int = 0
    last_ms: int = 0

    def take(self, at_ms: int, *, min_gap_ms: int = 0) -> bool:
        """Consume one token if the window has room *and* the min-gap has passed. Returns whether it did."""
        if at_ms - self.window_start_ms >= self.window_ms:
            self.window_start_ms = at_ms
            self.used = 0
        if self.used >= self.capacity:
            return False
        if min_gap_ms and self.last_ms and at_ms - self.last_ms < min_gap_ms:
            return False
        self.used += 1
        self.last_ms = at_ms
        return True

    def ready_at(self, at_ms: int, *, min_gap_ms: int = 0) -> int:
        """The earliest millisecond this bucket could take a token — the number the sender sleeps until."""
        start = self.window_start_ms if at_ms - self.window_start_ms < self.window_ms else at_ms
        by_window = start + self.window_ms if self.used >= self.capacity else at_ms
        by_gap = (self.last_ms + min_gap_ms) if (min_gap_ms and self.last_ms) else at_ms
        return max(by_window, by_gap)


@dataclass
class Buckets:
    """Every bucket one bot has, with the chat buckets kept per chat id."""

    global_: Bucket = field(default_factory=lambda: Bucket(GLOBAL_PER_SECOND, SECOND))
    chats: dict = field(default_factory=dict)

    def chat(self, chat_id: str, *, chat_type: str = "private") -> Bucket:
        key = str(chat_id)
        if key not in self.chats:
            if chat_type in ("group", "supergroup", "channel"):
                self.chats[key] = Bucket(GROUP_PER_MINUTE, MINUTE, last_ms=0)
            else:
                self.chats[key] = Bucket(CHAT_PER_SECOND, SECOND)
        return self.chats[key]

    @staticmethod
    def gap_for(chat_type: str) -> int:
        return GROUP_MIN_GAP_MS if chat_type in ("group", "supergroup", "channel") else 0


@dataclass(frozen=True)
class Job:
    """One message waiting to go out."""

    job_id: int
    chat_id: str
    chat_type: str = "private"
    priority: int = P_COMMAND_REPLY
    text: str = ""
    keyboard: list | None = None
    edit_message_id: int = 0
    attempts: int = 0
    created_ms: int = 0
    due_ms: int = 0
    method: str = "sendMessage"

    @property
    def priority_name(self) -> str:
        return PRIORITY_NAMES.get(self.priority, str(self.priority))


def order_key(job: Job) -> tuple:
    """Sort key: **priority first**, then due time, then age, then id.

    Priority leads, and the first version of this function had it the other way round (due time first) — which is
    the ordering that quietly breaks the phase's stated rule. With due-time-first, a broadcast scheduled one
    millisecond earlier than a fill goes out first, and inside a burst of channel messages a paying user's fill
    waits behind all of them. Readiness is already enforced by the `due_ms <= at_ms` filter in `plan()`, so `due_ms`
    here is only a tie-break *within* a priority — which is what it should have been.

    The remaining tie-breakers are not decoration either: two jobs of the same priority due in the same millisecond
    go out in the order they were created (`job_id`), because Telegram delivers in the order we send and our sort IS
    the user's reading order.
    """
    return (int(job.priority), int(job.due_ms or 0), int(job.created_ms or 0), int(job.job_id))


def order_findings(jobs: list, at_ms: int = 0) -> list:
    """Any way the queue's own promises are broken — the shape the gate and the canary check."""
    out = []
    ids = set()
    for j in jobs or []:
        if j.job_id in ids:
            out.append("job %s is queued twice" % j.job_id)
        ids.add(j.job_id)
        if j.chat_id in ("", None):
            out.append("job %s has no chat" % j.job_id)
        if j.priority not in PRIORITY_NAMES:
            out.append("job %s has an unknown priority %r" % (j.job_id, j.priority))
        if not (j.text or "").strip() and not j.edit_message_id:
            out.append("job %s carries nothing to send" % j.job_id)
        if len(j.text or "") > MAX_TEXT and j.method == "sendMessage":
            out.append("job %s is %d characters; the cap is %d" % (j.job_id, len(j.text), MAX_TEXT))
    if at_ms and any(int(j.due_ms or 0) > at_ms + 60_000 for j in jobs or []):
        out.append("a job is scheduled more than a minute ahead, which is a scheduler's job, not a queue's")
    return out


def plan(jobs: list, *, at_ms: int, buckets: Buckets, limit: int = 30) -> tuple[list, int]:
    """Which jobs to send now, in send order, and when to look again if there is nothing to do.

    Pure: it mutates *copies* of the bucket state (the caller owns the real ones) so a dry run cannot spend the
    bot's budget — which is what the gate's canary does: it plans the same queue twice and expects the same answer.

    The subtle part is that a blocked chat does not block the queue: a broadcast to a channel that is inside its
    per-minute window must not hold up a fill notification to somebody else. The scan therefore walks the whole
    queue in priority order and skips jobs whose chat is on cooldown, rather than stopping at the first one.
    """
    local: dict = {}
    for chat_id, b in (buckets.chats or {}).items():
        local[chat_id] = Bucket(b.capacity, b.window_ms, used=b.used, window_start_ms=b.window_start_ms,
                                last_ms=b.last_ms)
    g = Bucket(buckets.global_.capacity, buckets.global_.window_ms, used=buckets.global_.used,
               window_start_ms=buckets.global_.window_start_ms, last_ms=buckets.global_.last_ms)

    ready = [j for j in (jobs or []) if int(j.due_ms or 0) <= int(at_ms)]
    ready.sort(key=order_key)
    chosen: list = []
    for j in ready:
        if len(chosen) >= max(1, int(limit)):
            break
        if not g.take(int(at_ms)):
            break                                   # the global window is the hard ceiling; nothing goes out
        chat = local.setdefault(str(j.chat_id),
                                Bucket(CHAT_PER_SECOND, SECOND) if j.chat_type not in
                                ("group", "supergroup", "channel") else Bucket(GROUP_PER_MINUTE, MINUTE))
        if not chat.take(int(at_ms), min_gap_ms=Buckets.gap_for(j.chat_type)):
            # Undo the global token we optimistically took: a job that cannot go out must not spend headroom the
            # next job in the queue could use. (Found by the canary: a blocked channel was starving every private
            # reply behind it, one token per skipped job.)
            g.used -= 1
            continue
        chosen.append(j)
    if chosen:
        return chosen, 0
    waits = [int(at_ms) + 250]
    for chat_id, b in local.items():
        waits.append(b.ready_at(int(at_ms), min_gap_ms=0))
    waits.append(g.ready_at(int(at_ms)))
    for j in ready[:1]:
        waits.append(int(j.due_ms or 0))
    return [], max(250, min(waits) - int(at_ms))


def make_room_for_a_fill(jobs: list, *, at_ms: int, reserved: int = 2) -> list:
    """Drop the lowest-priority *broadcast* jobs to make room, and never anything addressed to a person.

    This runs when a fill notification arrives while the queue is full. The rule it encodes is the product rule:
    a user's own fill is the one message that must not be late, and a public-channel alert is the one message that
    can always wait — it is about a market that will still be interesting in thirty seconds.
    """
    if not jobs:
        return []
    kinds = [j for j in jobs if j.priority >= P_ALERT_CHANNEL]
    if len(jobs) < reserved + len(kinds):
        return []
    # Least important broadcast first: with `order_key` leading on priority, an ascending sort would hand us the
    # *most* important channel message as the victim, which is the opposite of the rule.
    victims = sorted(kinds, key=lambda j: (-int(j.priority), int(j.due_ms or 0), int(j.job_id)))
    return victims[:reserved]


@dataclass(frozen=True)
class SendResult:
    job_id: int
    ok: bool
    status: int = 0
    retry_after_s: int = 0
    note: str = ""


def should_retry(*, status: int, attempts: int, retry_after_s: int = 0) -> bool:
    """Whether a refused send is worth another attempt, and the queue's honest answer for the rest.

    429 is retried (with Telegram's own `retry_after`), 5xx is retried a few times, and a 400 is not retried at
    all: it means the message itself is malformed — a bad `parse_mode`, a keyboard the API refuses — and re-sending
    it three times just makes three failures. A blocked bot (`403`) is not retried either: the user blocked us, and
    the queue should drop the job rather than accumulate a debt that will never be paid.
    """
    if status == 429:
        return attempts < 6
    if 500 <= status < 600:
        return attempts < 4
    if status == 0:                                    # a network failure: we do not know if it arrived
        return attempts < 3
    return False


def retry_delay_ms(*, attempts: int, retry_after_s: int = 0) -> int:
    """`retry_after` when Telegram gives it, else exponential backoff starting at 1 s — capped at a minute."""
    if retry_after_s:
        return min(60 * SECOND, retry_after_s * SECOND)
    return min(60 * SECOND, SECOND * (2 ** max(0, min(attempts, 6))))


def drain_findings(results: list, *, at_ms: int, buckets: Buckets, jobs: list) -> list:
    """Post-hoc checks on one drain: what actually went out, in what order, and at what rate."""
    out = []
    sent = [r for r in results or [] if r.ok]
    for a, b in zip(sent, sent[1:]):
        ja = next((j for j in jobs if j.job_id == a.job_id), None)
        jb = next((j for j in jobs if j.job_id == b.job_id), None)
        if ja and jb and jb.priority < ja.priority and int(jb.created_ms or 0) > int(ja.created_ms or 0):
            out.append("job %s (%s) was sent after %s (%s) although it outranks it"
                       % (jb.job_id, jb.priority_name, ja.job_id, ja.priority_name))
    per_chat: dict = {}
    for r in sent:
        j = next((x for x in jobs if x.job_id == r.job_id), None)
        if j:
            per_chat.setdefault(str(j.chat_id), []).append(j)
    for chat_id, js in per_chat.items():
        if len(js) > 1 and js[0].chat_type in ("group", "supergroup", "channel"):
            out.append("chat %s received %d messages in one drain; the group limit is per minute" % (chat_id, len(js)))
    return out
