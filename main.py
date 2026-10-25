#!/usr/bin/env python3
"""SIJBA connector: chat with sijba through your own Telegram bot.

Inbound: long-polls the Telegram Bot API (no public endpoint needed) and
injects text from paired chats into Core via POST /api/message.
Outbound: registers with Core for replies whose source is "telegram" and
sends them back to the chat they came from.

Only paired chats are served: a chat pairs by sending `/pair <code>`; the
list lives in Core's store so it survives restarts.

Zero third-party deps — stdlib only.
"""
from __future__ import annotations

import hmac
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIJBA_URL = os.environ.get("SIJBA_URL", "http://127.0.0.1:8080").rstrip("/")
SIJBA_TOKEN = os.environ.get("SIJBA_TOKEN", "").strip()
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9102"))
HANDLER_ID = os.environ.get("HANDLER_ID", "telegram")
SOURCE = "telegram"
TTL_SECONDS = int(os.environ.get("TTL_SECONDS", "90"))
HEARTBEAT_EVERY = max(10, TTL_SECONDS // 6)
REGISTER_PRIORITY = float(os.environ.get("REGISTER_PRIORITY", "50"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PAIRING_CODE = os.environ.get("TELEGRAM_PAIRING_CODE", "").strip()
TARGET = os.environ.get("TELEGRAM_TARGET", "").strip()

POLL_TIMEOUT = 30
MESSAGE_LIMIT = 4096  # Telegram sendMessage limit
PAIR_MAX_TRIES = 5
PAIR_WINDOW = 600.0
STORE_NS, STORE_KEY = HANDLER_ID, "paired_chats"

_stop = threading.Event()
_paired: set[str] = set()
_paired_loaded = False
_pair_tries: dict[str, list[float]] = {}
_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"[sijba-telegram] {msg}", file=sys.stderr, flush=True)


def _default_advertise() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:{LISTEN_PORT}"
    except Exception:
        return f"http://127.0.0.1:{LISTEN_PORT}"


ADVERTISE_URL = os.environ.get("ADVERTISE_URL", "").rstrip("/") or _default_advertise()


# ------------------------------------------------------------------
# HTTP helpers
# ------------------------------------------------------------------

def _request(url: str, method: str, body: dict | None, headers: dict, timeout: float) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{method} {url.split('/bot')[0]}…: HTTP {exc.code} {err}") from exc


def core(method: str, path: str, body: dict | None = None) -> dict:
    return _request(f"{SIJBA_URL}{path}", method, body,
                    {"Authorization": f"Bearer {SIJBA_TOKEN}"}, 15)


def tg(method: str, body: dict | None = None, timeout: float = 15) -> dict:
    res = _request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", "POST", body or {}, {}, timeout)
    if not res.get("ok"):
        raise RuntimeError(f"telegram {method}: {res.get('description', res)}")
    return res.get("result")


# ------------------------------------------------------------------
# Pairing
# ------------------------------------------------------------------

def load_paired() -> None:
    global _paired_loaded
    try:
        value = core("GET", f"/api/store/{STORE_NS}/{STORE_KEY}").get("value") or []
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        value = []
    with _lock:
        _paired.clear()
        _paired.update(str(c) for c in value)
        _paired_loaded = True


def _save_paired() -> None:
    core("PUT", f"/api/store/{STORE_NS}/{STORE_KEY}", {"value": sorted(_paired)})


def is_paired(chat_id: str) -> bool:
    with _lock:
        return chat_id in _paired


def try_pair(chat_id: str, code: str, now: float | None = None) -> str:
    """Returns the reply text for a /pair attempt."""
    now = time.time() if now is None else now
    with _lock:
        tries = [t for t in _pair_tries.get(chat_id, []) if now - t < PAIR_WINDOW]
        if len(tries) >= PAIR_MAX_TRIES:
            _pair_tries[chat_id] = tries
            return "Too many attempts. Try again later."
        if not PAIRING_CODE or not hmac.compare_digest(code.encode(), PAIRING_CODE.encode()):
            tries.append(now)
            _pair_tries[chat_id] = tries
            return "Wrong code."
        _pair_tries.pop(chat_id, None)
        _paired.add(chat_id)
    _save_paired()
    return "Paired. Write anything and sijba answers here."


def unpair(chat_id: str) -> str:
    with _lock:
        _paired.discard(chat_id)
    _save_paired()
    return "Unpaired. Send /pair <code> to pair again."


# ------------------------------------------------------------------
# Inbound: Telegram -> Core
# ------------------------------------------------------------------

def to_core_message(msg: dict) -> dict:
    chat = msg["chat"]
    sender = msg.get("from") or {}
    chat_id = str(chat["id"])
    body = {
        "text": msg["text"],
        "source": SOURCE,
        "source_meta": {
            # chat_id buckets the transcript; Core may overwrite it on replies
            # routed through a plugin channel, telegram_chat_id survives.
            "chat_id": f"telegram:{chat_id}",
            "telegram_chat_id": chat_id,
            "chat_type": chat.get("type", "private"),
            "user": sender.get("username") or sender.get("first_name", ""),
            "user_id": str(sender.get("id", "")),
            "message_id": msg.get("message_id"),
        },
    }
    if TARGET:
        body["target"] = TARGET
    return body


def handle_update(update: dict) -> None:
    msg = update.get("message")
    if not msg or not isinstance(msg.get("text"), str):
        return
    chat_id = str(msg["chat"]["id"])
    text = msg["text"].strip()
    command, _, arg = text.partition(" ")
    command = command.split("@", 1)[0].lower()

    if command == "/pair":
        reply(chat_id, try_pair(chat_id, arg.strip()))
        return
    if not is_paired(chat_id):
        if command == "/start" or command.startswith("/"):
            reply(chat_id, "This is a private sijba bot. Send /pair <code> to connect this chat.")
        return
    if command == "/unpair":
        reply(chat_id, unpair(chat_id))
        return
    if command == "/start":
        reply(chat_id, "Connected to sijba. Just write.")
        return

    try:
        tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass
    core("POST", "/api/message", to_core_message(msg))


def reply(chat_id: str, text: str) -> None:
    try:
        tg("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as exc:
        _log(f"sendMessage failed: {exc}")


def poll_loop() -> None:
    offset = 0
    while not _stop.is_set():
        try:
            if not _paired_loaded:
                load_paired()
            updates = tg("getUpdates", {"timeout": POLL_TIMEOUT, "offset": offset,
                                        "allowed_updates": ["message"]}, timeout=POLL_TIMEOUT + 10)
        except Exception as exc:
            _log(f"poll error: {exc}")
            _stop.wait(5)
            continue
        for update in updates or []:
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception as exc:
                _log(f"update {update.get('update_id')} failed: {exc}")


# ------------------------------------------------------------------
# Outbound: Core reply -> Telegram
# ------------------------------------------------------------------

def chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def handle_invoke(envelope: dict) -> dict:
    meta = envelope.get("source_meta") or {}
    chat_id = str(meta.get("telegram_chat_id") or "")
    text = (envelope.get("pipeline_text") or envelope.get("text") or "").strip()
    if not chat_id or not text:
        return {"continue": True}
    if not is_paired(chat_id):
        _log(f"reply for unpaired chat {chat_id} dropped")
        return {"continue": True}
    for part in chunks(text):
        reply(chat_id, part)
    return {"continue": True}


# ------------------------------------------------------------------
# Core registration + HTTP server
# ------------------------------------------------------------------

def register() -> None:
    res = core("POST", "/api/register", {
        "id": HANDLER_ID,
        "url": ADVERTISE_URL,
        "direction": "outbound",
        "priority": REGISTER_PRIORITY,
        "match": {"source": [SOURCE]},
        "invoke_path": "/invoke",
        "timeout_seconds": 15,
        "ttl_seconds": TTL_SECONDS,
        "continue_on_match": True,
    })
    _log(f"registered: {res.get('status')}")


def _heartbeat_loop() -> None:
    while not _stop.wait(HEARTBEAT_EVERY):
        try:
            core("POST", f"/api/register/{HANDLER_ID}/heartbeat", {"ttl_seconds": TTL_SECONDS})
        except Exception as exc:
            _log(f"heartbeat failed: {exc} — re-registering")
            try:
                register()
            except Exception as exc2:
                _log(f"re-register failed: {exc2}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        pass

    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] in ("/health", "/"):
            self._json(200, {"ok": True, "id": HANDLER_ID})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/invoke":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            envelope = json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}")
            self._json(200, handle_invoke(envelope))
        except Exception as exc:
            _log(f"invoke error: {exc}")
            self._json(200, {"continue": True})


def main() -> None:
    if not BOT_TOKEN or not PAIRING_CODE or not SIJBA_TOKEN:
        _log("TELEGRAM_BOT_TOKEN, TELEGRAM_PAIRING_CODE and SIJBA_TOKEN are required")
        sys.exit(1)
    me = tg("getMe")
    _log(f"bot @{me.get('username')} id={HANDLER_ID} advertise={ADVERTISE_URL}")
    # getUpdates does not work while a webhook is set on the bot.
    tg("deleteWebhook", {"drop_pending_updates": False})

    register()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True).start()
    threading.Thread(target=poll_loop, name="poll", daemon=True).start()

    def _shutdown(*_args) -> None:
        _stop.set()
        try:
            core("DELETE", f"/api/register/{HANDLER_ID}")
        except Exception as exc:
            _log(f"unregister failed: {exc}")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    _log("ready")
    server.serve_forever()


if __name__ == "__main__":
    main()
