"""A standard-library WebSocket client, written because a stalled socket must be distinguishable from a
quiet market — which is the whole failure mode P05's role brief is about.

Deliberately small and deliberately not a general library: text frames in, one JSON callback, ping/pong,
close handshake, fragmentation. No third-party dependency, because this process is the one that must not
acquire a CVE on the venue's network path.

The two counters that make silent-death detection possible live on the client, not in the caller:
`last_msg_at` (updated by ANY frame, including a pong, so a socket that is alive but idle is not confused
with a socket that is dead and idle) and `frames`/`bytes`. `age_s()` is the number the lag alarm reads.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_FRAME = 8 * 1024 * 1024


class WsError(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    buf = b""
    while len(buf) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("incomplete frame with %d of %d bytes" % (len(buf), n))
        sock.settimeout(min(left, 1.0))
        try:
            chunk = sock.recv(n - len(buf))
        except (TimeoutError, ssl.SSLWantReadError):
            continue
        except socket.timeout:
            continue
        if not chunk:
            raise WsError("socket closed mid-frame (got %d of %d bytes)" % (len(buf), n))
        buf += chunk
    return buf


def _read_frame(sock: socket.socket, deadline: float) -> tuple[bool, int, bytes]:
    hdr = _recv_exact(sock, 2, deadline)
    b0, b1 = hdr[0], hdr[1]
    fin, op = bool(b0 & 0x80), b0 & 0x0F
    masked, ln = bool(b1 & 0x80), b1 & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", _recv_exact(sock, 2, deadline))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", _recv_exact(sock, 8, deadline))[0]
    if ln > MAX_FRAME:
        raise WsError("frame too large: %d bytes (cap %d)" % (ln, MAX_FRAME))
    key = _recv_exact(sock, 4, deadline) if masked else None
    payload = _recv_exact(sock, ln, deadline) if ln else b""
    if key:
        payload = bytes(c ^ key[i % 4] for i, c in enumerate(payload))
    return fin, op, payload


def _mask(op: int, payload: bytes) -> bytes:
    key = os.urandom(4)
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | op, 0x80 | n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | op, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | op, 0x80 | 127, n)
    return head + key + bytes(c ^ key[i % 4] for i, c in enumerate(payload))


class WsClient:
    """`on_message(dict)` is called for every JSON text frame; every other frame type is handled here.

    A callback that raises is RECORDED and swallowed (`handler_errors`), not propagated: a malformed payload
    from the venue must not take the connection down, because the connection is the only thing that knows how
    stale the market is. The error count is what pages somebody, not the exception.
    """

    def __init__(self, url: str, on_message=None, *, timeout: float = 10.0, ping_every: float = 15.0,
                 opener=None):
        self.url = url
        self.on_message = on_message or (lambda m: None)
        self.timeout, self.ping_every = timeout, ping_every
        self._opener = opener                      # tests inject a socket pair; production uses _connect
        self.sock: socket.socket | None = None
        self.connected_at: float | None = None
        self.last_msg_at: float | None = None      # any frame, including a pong
        self.last_text_at: float | None = None     # data only: this is the one the tape watches
        self.frames = self.bytes_in = self.pings = self.pongs = 0
        self.handler_errors: list[str] = []
        self.resumed = 0                            # resyncs issued after a gap or a reconnect
        self.closed_reason: str | None = None
        self._frag: list[bytes] = []
        self._frag_op: int | None = None

    # ------------------------------------------------------------------ connect
    def connect(self) -> None:
        u = urlsplit(self.url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=self.timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, u.hostname, key))
        raw.sendall(req.encode())
        raw.settimeout(self.timeout)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = raw.recv(4096)
            if not chunk:
                raise WsError("handshake closed: %r" % buf[:200])
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if " 101 " not in head.split("\r\n", 1)[0] + " ":
            raise WsError("handshake refused: %s" % head.split("\r\n", 1)[0])
        self.sock = raw
        self.connected_at = self.last_msg_at = self.last_text_at = time.monotonic()
        self.closed_reason = None

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall(_mask(OP_CLOSE, struct.pack("!H", 1000)))
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None
            self.closed_reason = self.closed_reason or "local close"

    # ------------------------------------------------------------------ i/o
    def send_json(self, obj) -> None:
        if self.sock is None:
            raise WsError("not connected")
        self.sock.sendall(_mask(OP_TEXT, json.dumps(obj).encode()))

    def subscribe_market(self, assets: list[str]) -> None:
        """The CLOB market channel. The field is `assets_ids` and the type is `market` — verified against the
        live socket in P01 and re-verified by tools/p05-capture-fixtures.py; the shape is not guessed."""
        self.send_json({"assets_ids": list(assets), "type": "market"})

    def poll(self, max_wait: float = 1.0) -> int:
        """Read whatever arrives within `max_wait`. Returns the number of data frames delivered.

        Pings are answered here so the venue's own idle timer never closes us, and a pong counts as liveness
        but not as data: those two facts are the difference between "quiet market" and "dead ingest".
        """
        if self.sock is None:
            raise WsError("not connected")
        deadline = time.monotonic() + max_wait
        n = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            # Re-read the socket every iteration, and treat a vanished one as a close. `close()` sets
            # `self.sock = None` from another thread — the shutdown path, and the chaos harness's kill — and the
            # entry guard above cannot see it: the loop was already inside. Reading the attribute once per
            # iteration turns `AttributeError: 'NoneType' object has no attribute 'settimeout'` (which escapes as
            # a thread crash, taking the ingest down over a routine disconnect) into the WsError that every
            # caller already handles as "transport gone, resync and reconnect".
            sock = self.sock
            if sock is None:
                self.closed_reason = self.closed_reason or "closed locally"
                raise WsError(self.closed_reason)
            if self.ping_every and self.last_msg_at and time.monotonic() - self.last_msg_at > self.ping_every:
                try:
                    sock.sendall(_mask(OP_PING, b"hb"))
                except OSError:
                    raise WsError("ping failed: transport gone") from None
                self.pings += 1
            try:
                sock.settimeout(min(left, 0.5))
                fin, op, payload = _read_frame(sock, deadline)
            except TimeoutError:
                continue
            except (ssl.SSLWantReadError, socket.timeout):
                continue
            except OSError as e:
                # A socket closed from the other thread reads as EBADF here, not as a clean FIN. If the handle
                # is gone we call that a local close and report it as a WsError with a reason, because the
                # alternative is an OSError with no context reaching the reconnect loop and an outage page that
                # says "transport error" about a shutdown we asked for.
                if self.sock is None:
                    self.closed_reason = self.closed_reason or "closed locally"
                    raise WsError(self.closed_reason) from None
                self.closed_reason = str(e)[:160]
                raise
            except WsError as e:
                self.closed_reason = str(e)[:160]
                raise
            self.frames += 1
            self.bytes_in += len(payload)
            self.last_msg_at = time.monotonic()
            if op == OP_PING:
                self.sock.sendall(_mask(OP_PONG, payload[:125]))
                continue
            if op == OP_PONG:
                self.pongs += 1
                continue
            if op == OP_CLOSE:
                self.closed_reason = "peer close %s" % (payload[:2].hex() or "?")
                raise WsError("peer closed: %s" % (payload[:60].decode("utf-8", "replace") or self.closed_reason))
            if op == OP_CONT:
                # Fragmentation is handled because a `book` snapshot for a deep market is the one message
                # shape here that can exceed a frame: dropping the tail would leave a book that looks complete
                # and is not, which is the worst possible failure for a price ladder.
                if not self._frag:
                    raise WsError("continuation frame with no start frame")
                self._frag.append(payload)
                if not fin:
                    continue
                text = b"".join(self._frag)
                self._frag, self._frag_op = [], None
            elif op in (OP_TEXT, OP_BINARY):
                if self._frag:
                    raise WsError("new message started while a fragment was pending")
                if not fin:
                    self._frag, self._frag_op = [payload], op
                    continue
                text = payload
            else:
                continue
            self.last_text_at = time.monotonic()
            n += 1
            try:
                self.on_message(json.loads(text.decode("utf-8")))
            except Exception as e:                                        # noqa: BLE001 - see class docstring
                self.handler_errors.append("%s: %s" % (type(e).__name__, str(e)[:120]))
        return n

    def age_s(self, now: float | None = None) -> float | None:
        """Seconds since ANY frame. None before the first one, so a caller cannot read 0 as 'fresh'."""
        if self.last_msg_at is None:
            return None
        return ((time.monotonic() if now is None else now) - self.last_msg_at)

    def data_age_s(self, now: float | None = None) -> float | None:
        if self.last_text_at is None:
            return None
        return ((time.monotonic() if now is None else now) - self.last_text_at)
