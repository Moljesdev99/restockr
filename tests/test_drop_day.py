"""Drop-day delivery: in-stock alerts repeat on Telegram even when ntfy fails."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import restockr_core as core

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(core, "TEST_MODE", False)
    clock = {"now": datetime(2026, 11, 19, 2, 0, tzinfo=core.TZ)}
    monkeypatch.setattr(core, "now_mx", lambda: clock["now"])
    sent = []
    monkeypatch.setattr(core, "tg_send_card", lambda info, st, product=None, **k: sent.append((product["id"], k.get("reply_markup"))) or True)
    monkeypatch.setattr(core, "tg_send", lambda *a, **k: True)
    monkeypatch.setattr(core, "ntfy_card", lambda *a, **k: (False, 429))  # box: ntfy rate-limited
    monkeypatch.setattr(core, "ntfy", lambda *a, **k: (False, 429))
    raw = json.loads((ROOT / "config.json").read_text())
    cfg = core.parse_config({"topic": "t", **raw}, require_topic=False)
    return cfg, clock, sent


@pytest.mark.parametrize("pid", ["chromatic", "chromatic-case"])
def test_in_stock_repeats_every_two_minutes_with_ntfy_down(env, pid):
    cfg, clock, sent = env
    product = next(p for p in cfg["products"] if p["id"] == pid)
    state = core.default_state()
    core.product_state(state, product)["last_status"] = "out_of_stock"
    info = {"status": "in_stock", "product_ok": True, "price": "US$1"}
    for i in range(4):
        core.handle_result(cfg, state, product, info)
        clock["now"] += timedelta(seconds=core.IN_STOCK_REPEAT_S + 1)
    mine = [s for s in sent if s[0] == pid]
    assert len(mine) == 4
    # buttons on every repeat
    assert all("rs|b|" + pid in json.dumps(m[1]) for m in mine)
    assert core.product_state(state, product)["in_stock_alerts_sent"] == 4


def test_exhausted_events_are_dropped(env):
    cfg, clock, sent = env
    product = next(p for p in cfg["products"] if p["id"] == "chromatic")
    state = core.default_state()
    core.emit_alert(cfg, state, product, kind="heartbeat", use_card=False, text="x")
    for _ in range(core.DELIVERY_MAX_ATTEMPTS + 2):
        clock["now"] += timedelta(seconds=core.DELIVERY_BACKOFF_CAP_S + 1)
        core.flush_pending(cfg, state, cfg["products"])
    assert state["pending_events"] == []


def test_restock_day_cadence_and_backoff_cap():
    raw = json.loads((ROOT / "config.json").read_text())
    products = core.parse_config({"topic": "t", **raw}, require_topic=False)["products"]
    drop = datetime(2026, 11, 19, 3, 0, tzinfo=core.DISPLAY_TZ)
    assert core.check_interval_range(now=drop, products=products) == (60.0, 75.0)
    assert core.REMINDER_DAY_BACKOFF_CAP_S == 30


def test_box_skips_reminder_and_heartbeat(env, monkeypatch):
    """Only the cloud watcher sends the 06:00 reminder and the 09:00 heartbeat."""
    cfg, clock, sent = env
    product = next(p for p in cfg["products"] if p["id"] == "chromatic")
    clock["now"] = datetime(2026, 11, 19, 9, 1, tzinfo=core.TZ)
    calls = []
    monkeypatch.setattr(core, "emit_alert", lambda *a, **k: calls.append(k.get("kind")))
    state = core.default_state()
    box = dict(cfg, daily_notices=False)
    core.maybe_specials(box, state, product, core.product_state(state, product), "out_of_stock")
    assert calls == []
    core.maybe_specials(cfg, state, product, core.product_state(state, product), "out_of_stock")
    assert calls == ["reminder", "heartbeat"]
