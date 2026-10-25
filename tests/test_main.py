import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import main as bot  # noqa: E402


@pytest.fixture(autouse=True)
def fake_io(monkeypatch):
    calls = {"tg": [], "core": [], "store": None}

    def tg(method, body=None, timeout=15):
        calls["tg"].append((method, body))
        return True

    def core(method, path, body=None):
        calls["core"].append((method, path, body))
        if path.startswith("/api/store/") and method == "PUT":
            calls["store"] = body["value"]
        return {}

    monkeypatch.setattr(bot, "tg", tg)
    monkeypatch.setattr(bot, "core", core)
    monkeypatch.setattr(bot, "PAIRING_CODE", "sesam")
    monkeypatch.setattr(bot, "TARGET", "")
    bot._paired.clear()
    bot._pair_tries.clear()
    return calls


def update(text, chat=42):
    return {"update_id": 1, "message": {"message_id": 7, "text": text,
                                        "chat": {"id": chat, "type": "private"},
                                        "from": {"id": chat, "username": "rb"}}}


def sent(calls):
    return [b["text"] for m, b in calls["tg"] if m == "sendMessage"]


def test_unpaired_text_is_ignored_and_not_forwarded(fake_io):
    bot.handle_update(update("hallo"))
    assert fake_io["core"] == [] and sent(fake_io) == []


def test_unpaired_start_gets_pairing_hint(fake_io):
    bot.handle_update(update("/start"))
    assert "/pair" in sent(fake_io)[0]


def test_pair_with_right_code_persists_and_forwards_afterwards(fake_io):
    bot.handle_update(update("/pair sesam"))
    assert sent(fake_io)[-1].startswith("Paired")
    assert fake_io["store"] == ["42"]
    bot.handle_update(update("wie ist das wetter"))
    method, path, body = fake_io["core"][-1]
    assert (method, path) == ("POST", "/api/message")
    assert body["source"] == "telegram" and body["text"] == "wie ist das wetter"
    assert body["source_meta"]["telegram_chat_id"] == "42"
    assert body["source_meta"]["chat_id"] == "telegram:42"
    assert "target" not in body


def test_target_is_passed_when_configured(fake_io, monkeypatch):
    monkeypatch.setattr(bot, "TARGET", "router-llm")
    bot._paired.add("42")
    bot.handle_update(update("x"))
    assert fake_io["core"][-1][2]["target"] == "router-llm"


def test_wrong_code_is_rate_limited(fake_io):
    for _ in range(bot.PAIR_MAX_TRIES):
        assert bot.try_pair("9", "nope", now=1000.0) == "Wrong code."
    assert bot.try_pair("9", "sesam", now=1001.0).startswith("Too many")
    assert bot.try_pair("9", "sesam", now=1000.0 + bot.PAIR_WINDOW + 1).startswith("Paired")


def test_unpair(fake_io):
    bot._paired.add("42")
    bot.handle_update(update("/unpair"))
    assert fake_io["store"] == [] and not bot.is_paired("42")


def test_invoke_replies_to_paired_chat_only(fake_io):
    bot._paired.add("42")
    env = {"source": "telegram", "pipeline_text": "Antwort",
           "source_meta": {"chat_id": "obsidian", "telegram_chat_id": "42"}}
    assert bot.handle_invoke(env) == {"continue": True}
    assert sent(fake_io) == ["Antwort"]
    env["source_meta"]["telegram_chat_id"] = "666"
    bot.handle_invoke(env)
    assert sent(fake_io) == ["Antwort"]


def test_long_reply_is_split():
    parts = bot.chunks("a" * 5000 + "\n" + "b" * 10)
    assert all(len(p) <= bot.MESSAGE_LIMIT for p in parts)
    assert "".join(parts).replace("\n", "") == "a" * 5000 + "b" * 10


def test_connector_json_fields_are_read_by_main():
    spec = json.loads((ROOT / "connector.json").read_text())
    used = set(re.findall(r'environ\.get\("([A-Z0-9_]+)"', (ROOT / "main.py").read_text()))
    assert spec["type"] == "telegram" and spec["port"] == 9102
    for f in spec["fields"]:
        assert f["name"] in used, f["name"]
