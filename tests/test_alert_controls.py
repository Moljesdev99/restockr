"""Inline alert buttons (Bought it / Not buying / Keep alerting) and commands. No network."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import restockr_core as core

CHAT = "12345"
PROD = {"id": "chromatic", "name": "Game Boy Chromatic Codex", "short_name": "the Chromatic Codex",
        "url": "https://openai.com/supply/co-lab/chromatic/", "emoji": "🎮", "type": "restock"}
OTHER = {"id": "disney-ezra", "name": "Ezra Legacy Lightsaber Set", "url": "https://example.com/ezra",
         "emoji": "⚔️", "type": "restock"}


class FakeTG:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def __call__(self, method, fields, file_field=None, *, with_chat=True):
        f = dict(fields)
        if with_chat:
            f["chat_id"] = CHAT
        self.calls.append((method, f))
        if method == "getUpdates":
            off = int(f.get("offset") or 0)
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= off]}
        return {"ok": True, "result": True}

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(core, "TEST_MODE", False)
    monkeypatch.setattr(core, "_tg_creds", lambda: ("TOKEN", CHAT))
    fake = FakeTG()
    monkeypatch.setattr(core, "_tg_post", fake)
    monkeypatch.setattr(core, "_last_poll", 0.0)
    monkeypatch.setattr(core, "TG_REPLY_COMMANDS", True)
    state = core.load_state([PROD, OTHER])
    cfg = {"products": [PROD, OTHER], "topic": "restockr-no-topic", "_ntfy_enabled": False}
    core.set_poll_context(cfg, state, [PROD, OTHER])
    return fake, state, cfg


def tap(fake, uid, action, pid="chromatic", chat=CHAT):
    fake.updates.append({"update_id": uid, "callback_query": {
        "id": f"cq{uid}", "data": f"rs|{action}|{pid}",
        "message": {"message_id": 77, "chat": {"id": int(chat)}}}})


def cmd(fake, uid, text, chat=CHAT, age=0):
    fake.updates.append({"update_id": uid, "message": {
        "message_id": 90 + uid, "chat": {"id": int(chat)}, "date": int(time.time()) - age, "text": text}})


def in_stock(cfg, state, product=PROD):
    core.handle_result(cfg, state, product, {"status": "in_stock"})


def test_keyboard_has_link_and_three_buttons():
    kb = core.build_alert_keyboard(PROD)["inline_keyboard"]
    assert kb[0][0]["url"] == PROD["url"]
    labels = [b["text"] for row in kb[1:] for b in row]
    assert labels == ["✅ Bought it", "🚫 Not buying", "🔔 Keep alerting"]
    for row in kb[1:]:
        for b in row:
            assert len(b["callback_data"].encode()) <= 64


def test_long_ids_fit_callback_data():
    long = {**PROD, "id": "x" * 80}
    kb = core.build_alert_keyboard(long)["inline_keyboard"]
    token = kb[1][0]["callback_data"].split("|", 2)[2]
    assert len(kb[1][0]["callback_data"].encode()) <= 64
    assert core._find_product([long], token) is long


def test_in_stock_card_carries_buttons(env):
    fake, state, cfg = env
    in_stock(cfg, state)
    sends = [f for m, f in fake.calls if m in ("sendPhoto", "sendMessage")]
    assert sends
    kb = json.loads(sends[-1]["reply_markup"])["inline_keyboard"]
    assert any(b.get("callback_data") == "rs|b|chromatic" for row in kb for b in row)


def test_sold_out_card_has_no_controls(env):
    fake, state, cfg = env
    core.product_state(state, PROD)["last_status"] = "in_stock"
    core.handle_result(cfg, state, PROD, {"status": "out_of_stock"})
    sends = [f for m, f in fake.calls if m in ("sendPhoto", "sendMessage")]
    kb = json.loads(sends[-1]["reply_markup"])["inline_keyboard"]
    assert all("callback_data" not in b for row in kb for b in row)


def test_bought_it_mutes_and_edits_message(env):
    fake, state, cfg = env
    tap(fake, 1, "b")
    assert core.poll_telegram(force=True) == 1
    assert core.alert_mode(state, PROD) == "bought"
    assert "answerCallbackQuery" in fake.methods()
    edit = [f for m, f in fake.calls if m == "editMessageReplyMarkup"][-1]
    assert "Bought" in edit["reply_markup"] and "rs|r|chromatic" in edit["reply_markup"]
    fake.calls.clear()
    in_stock(cfg, state)
    assert not [m for m in fake.methods() if m in ("sendPhoto", "sendMessage")]
    # Other products are unaffected.
    in_stock(cfg, state, OTHER)
    assert [m for m in fake.methods() if m in ("sendPhoto", "sendMessage")]


def test_not_buying_mutes_waiting_room_too(env):
    fake, state, cfg = env
    tap(fake, 1, "s")
    core.poll_telegram(force=True)
    fake.calls.clear()
    core.handle_queue(cfg, state, PROD, {"status": "queue"})
    assert not [m for m in fake.methods() if m in ("sendPhoto", "sendMessage")]


def test_mute_drops_pending_alerts(env):
    fake, state, cfg = env
    state["pending_events"] = [{"watch_id": "chromatic", "kind": "in_stock"},
                               {"watch_id": "chromatic", "kind": "heartbeat"},
                               {"watch_id": "disney-ezra", "kind": "in_stock"}]
    core.set_alert_mode(state, PROD, "bought")
    assert [(e["watch_id"], e["kind"]) for e in state["pending_events"]] == [
        ("chromatic", "heartbeat"), ("disney-ezra", "in_stock")]


def test_default_cap_and_keep_alerting_unlimited(env, monkeypatch):
    fake, state, cfg = env
    monkeypatch.setattr(core, "IN_STOCK_REPEAT_S", 0)
    ps = core.product_state(state, PROD)
    ps["last_status"] = "in_stock"
    ps["in_stock_alerts_sent"] = core.IN_STOCK_ALERT_MAX
    fake.calls.clear()
    in_stock(cfg, state)
    assert not [m for m in fake.methods() if m in ("sendPhoto", "sendMessage")]
    assert not core.any_in_stock_repeat(state, [PROD], {"chromatic": {"status": "in_stock"}})
    tap(fake, 1, "k")
    core.poll_telegram(force=True)
    fake.calls.clear()
    in_stock(cfg, state)
    assert [m for m in fake.methods() if m in ("sendPhoto", "sendMessage")]
    assert core.any_in_stock_repeat(state, [PROD], {"chromatic": {"status": "in_stock"}})


def test_keep_survives_sell_out_and_restock(env):
    fake, state, cfg = env
    core.set_alert_mode(state, PROD, "keep")
    in_stock(cfg, state)
    core.handle_result(cfg, state, PROD, {"status": "out_of_stock"})
    assert core.alert_mode(state, PROD) == "keep"


def test_taps_from_other_chats_are_ignored(env):
    fake, state, cfg = env
    tap(fake, 1, "b", chat="999")
    core.poll_telegram(force=True)
    assert core.alert_mode(state, PROD) is None


def test_each_update_handled_once_and_not_confirmed_early(env):
    fake, state, cfg = env
    tap(fake, 5, "b")
    core.poll_telegram(force=True)
    cmd(fake, 6, "/reset chromatic")
    assert core.poll_telegram(force=True) == 1
    assert core.alert_mode(state, PROD) is None
    assert core.poll_telegram(force=True) == 0
    # Nothing confirmed yet, so a second watcher can still read both updates.
    assert not state.get("tg_confirm_offset")
    assert "offset" not in [f for m, f in fake.calls if m == "getUpdates"][-1]


def test_old_updates_are_confirmed_after_retention(env, monkeypatch):
    fake, state, cfg = env
    tap(fake, 5, "b")
    core.poll_telegram(force=True)
    state["tg_seen"]["5"] = time.time() - core.TG_UPDATE_RETAIN_S - 1
    core.poll_telegram(force=True)
    assert state["tg_confirm_offset"] == 6
    core.poll_telegram(force=True)
    assert [f for m, f in fake.calls if m == "getUpdates"][-1]["offset"] == 6


def test_second_watcher_applies_same_taps(env, tmp_path, monkeypatch):
    fake, state, cfg = env
    tap(fake, 1, "s")
    core.poll_telegram(force=True)
    # Second watcher with its own state reads the same, unconfirmed queue.
    monkeypatch.setattr(core, "STATE_PATH", tmp_path / "state2.json")
    state2 = core.load_state([PROD, OTHER])
    core.set_poll_context(cfg, state2, [PROD, OTHER])
    core.poll_telegram(force=True)
    assert core.alert_mode(state2, PROD) == "skip"


def test_status_and_reset_commands(env):
    fake, state, cfg = env
    core.set_alert_mode(state, PROD, "bought")
    cmd(fake, 1, "/status")
    core.poll_telegram(force=True)
    sent = [f for m, f in fake.calls if m == "sendMessage"][-1]["text"]
    assert "Restockr status" in sent and "bought" in sent
    cmd(fake, 2, "/reset all")
    core.poll_telegram(force=True)
    assert core.alert_mode(state, PROD) is None
    assert "back on" in [f for m, f in fake.calls if m == "sendMessage"][-1]["text"]


def test_backup_watcher_does_not_reply_and_stale_commands_are_silent(env, monkeypatch):
    fake, state, cfg = env
    monkeypatch.setattr(core, "TG_REPLY_COMMANDS", False)
    cmd(fake, 1, "/reset all")
    core.poll_telegram(force=True)
    monkeypatch.setattr(core, "TG_REPLY_COMMANDS", True)
    cmd(fake, 2, "/status", age=3600)
    core.poll_telegram(force=True)
    assert "sendMessage" not in fake.methods()


def test_muted_watch_checked_rarely_and_heartbeat_label(env):
    fake, state, cfg = env
    core.set_alert_mode(state, PROD, "bought")
    t = 1_000_000.0
    assert core.should_check(state, PROD, now=t)
    assert not core.should_check(state, PROD, now=t + 60)
    assert core.should_check(state, PROD, now=t + core.MUTED_CHECK_S)
    assert core.should_check(state, OTHER, now=t + 60)
    assert "Bought" in core.heartbeat_watch_status(PROD, core.product_state(state, PROD))


def test_poll_tolerates_errors(env, monkeypatch):
    fake, state, cfg = env
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(core, "_tg_post", boom)
    assert core.poll_telegram(force=True) == 0
