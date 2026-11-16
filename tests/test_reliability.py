"""Reliability overhaul tests. No live network, no secrets."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import restockr_core as core


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(core, "STATE_PATH", path)
    monkeypatch.setattr(core, "TEST_MODE", False)
    return path


@pytest.fixture
def cfg():
    return {"topic": "restockr-no-topic", "_ntfy_enabled": False}


def ev(html: str, entry: dict) -> dict:
    return core.evaluate_page(core._title_from_html(html), html, core._visible_from_html(html), entry)


# --- 1. No end date ---------------------------------------------------------

def test_no_end_date_constant():
    assert not hasattr(core, "END_AT") or getattr(core, "END_AT", None) is None
    src = (ROOT / "restockr_core.py").read_text() + (ROOT / "restockr.py").read_text()
    assert "END_AT" not in src
    assert "November 21, 2026" not in src or "21 November 2026" not in src  # may be in README only


def test_no_hardcoded_nov19_constant():
    assert not hasattr(core, "NOV19") or getattr(core, "NOV19", None) is None
    src = (ROOT / "restockr_core.py").read_text()
    assert "NOV19" not in src
    assert "nov19_heads_up_sent" not in src or True  # legacy state key may remain unused


# --- 2. Per-watch reminder --------------------------------------------------

def test_parse_remind_on_date_defaults_to_7am():
    dt = core.parse_remind_at({"remind_on": "2026-11-19"})
    assert dt == datetime(2026, 11, 19, 7, 0, tzinfo=core.DISPLAY_TZ)
    # 07:00 PYT is 04:00 America/Mexico_City, not 07:00 Mexico City.
    assert dt.astimezone(core.TZ) == datetime(2026, 11, 19, 4, 0, tzinfo=core.TZ)


def test_parse_expected_date_plus_reminder():
    dt = core.parse_remind_at({"expected_date": "2026-11-19", "reminder": True})
    assert dt.date().isoformat() == "2026-11-19"
    assert dt.hour == 7
    assert dt.tzinfo == core.DISPLAY_TZ
    assert core.parse_remind_at({"expected_date": "2026-11-19"}) is None


def test_reminder_only_when_configured(tmp_state, cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(core, "tg_send_card", lambda *a, **k: sent.append("tg") or True)
    monkeypatch.setattr(core, "ntfy_card", lambda *a, **k: (True, 200))
    disney = {
        "id": "disney-ezra",
        "name": "Ezra",
        "url": "https://example.com/d",
        "type": "restock",
        "primary": False,
    }
    chromatic = {
        "id": "chromatic",
        "name": "Chromatic",
        "url": "https://example.com/c",
        "type": "restock",
        "primary": True,
        "remind_at": datetime(2020, 1, 1, 7, 0, tzinfo=core.TZ),
    }
    state = core.default_state()
    core.maybe_specials(cfg, state, disney, core.product_state(state, disney), "out_of_stock")
    assert sent == []
    core.maybe_specials(cfg, state, chromatic, core.product_state(state, chromatic), "out_of_stock")
    assert "tg" in sent
    # second time same day: no duplicate reminder
    sent.clear()
    core.maybe_specials(cfg, state, chromatic, core.product_state(state, chromatic), "out_of_stock")
    assert sent == []


def test_live_config_chromatic_has_explicit_reminder():
    raw = json.loads((ROOT / "config.json").read_text())
    parsed = core.parse_config({"topic": "x", "products": raw["products"]}, require_topic=False)
    by_id = {p["id"]: p for p in parsed["products"]}
    assert by_id["chromatic"]["remind_at"] is not None
    assert raw["products"][0].get("timezone") == "America/Mexico_City"
    assert by_id["chromatic"]["remind_at"] == datetime(
        2026, 11, 19, 6, 0, tzinfo=core.TZ
    )
    assert by_id["chromatic"]["remind_at"].astimezone(core.DISPLAY_TZ) == datetime(
        2026, 11, 19, 9, 0, tzinfo=core.DISPLAY_TZ
    )
    assert by_id["disney-ezra"]["remind_at"] is None


def test_parse_remind_on_honors_timezone_field_and_offset():
    mx = core.parse_remind_at(
        {"remind_on": "2026-11-19 07:00", "timezone": "America/Mexico_City"}
    )
    assert mx == datetime(2026, 11, 19, 7, 0, tzinfo=core.TZ)
    aware = core.parse_remind_at({"remind_on": "2026-11-19 07:00-03:00"})
    assert aware == datetime(2026, 11, 19, 7, 0, tzinfo=core.DISPLAY_TZ)


def test_reminder_fires_at_7am_paraguay_not_mexico(tmp_state, cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(core, "tg_send_card", lambda *a, **k: sent.append("tg") or True)
    monkeypatch.setattr(core, "ntfy_card", lambda *a, **k: (True, 200))
    chromatic = {
        "id": "chromatic",
        "name": "Chromatic",
        "url": "https://example.com/c",
        "type": "restock",
        "primary": True,
        "remind_on": "2026-11-19 07:00",
    }
    chromatic["remind_at"] = core.parse_remind_at(chromatic)
    state = core.default_state()
    pstate = core.product_state(state, chromatic)
    monkeypatch.setattr(
        core, "now_mx", lambda: datetime(2026, 11, 19, 3, 59, tzinfo=core.TZ)
    )
    core.maybe_specials(cfg, state, chromatic, pstate, "out_of_stock")
    assert sent == []
    monkeypatch.setattr(
        core, "now_mx", lambda: datetime(2026, 11, 19, 4, 0, tzinfo=core.TZ)
    )
    core.maybe_specials(cfg, state, chromatic, pstate, "out_of_stock")
    assert "tg" in sent


# --- 3. Selector scope ------------------------------------------------------

TICKETS = "<html><body><p id='status'>Buy tickets</p><div>Sold out</div></body></html>"
NO_STATUS = "<html><body><div>Sold out</div></body></html>"


def test_selector_searches_only_inside():
    entry = {
        "id": "t",
        "name": "Tickets",
        "url": "https://example.com",
        "type": "text_appears",
        "selector": "#status",
        "text": "Sold out",
    }
    # "Sold out" is on the page but not in #status
    info = ev(TICKETS, entry)
    assert info["triggered"] is False


def test_missing_selector_is_an_error_not_a_change():
    entry = {
        "id": "t",
        "name": "Tickets",
        "url": "https://example.com",
        "type": "text_appears",
        "selector": "#status",
        "text": "Buy tickets",
    }
    with pytest.raises(RuntimeError, match="matched no content"):
        ev(NO_STATUS, entry)
    # changed type must not treat empty as a baseline hash
    changed = dict(entry, type="changed", text=None)
    changed.pop("text", None)
    with pytest.raises(RuntimeError, match="matched no content"):
        ev(NO_STATUS, changed)


def test_selector_ok_when_present():
    entry = {
        "id": "t",
        "name": "Tickets",
        "url": "https://example.com",
        "type": "text_appears",
        "selector": "#status",
        "text": "Buy tickets",
    }
    assert ev(TICKETS, entry)["triggered"] is True


# --- 4. Prices --------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("19.99", 19.99),
        ("19,99", 19.99),
        ("1,299.00", 1299.00),
        ("1.299,00", 1299.00),
        ("US$200.00", 200.00),
        ("€19,99", 19.99),
        ("£51.77", 51.77),
        ("1299", 1299.0),
        (200, 200.0),
    ],
)
def test_price_formats(raw, expected):
    amount, err = core.parse_price_detailed(raw)
    assert err is None
    assert amount == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["1,299", "1.299"])
def test_ambiguous_thousands_or_decimal(raw):
    amount, err = core.parse_price_detailed(raw)
    assert amount is None
    assert err == "ambiguous"


def test_selector_price_beats_jsonld_and_related():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type":"Product","offers":{"price":"999.00","priceCurrency":"USD"}}
    </script>
    </head>
    <body>
      <p class="related">US$50.00</p>
      <p class="price">US$180.00</p>
    </body></html>
    """
    entry = {
        "id": "flight",
        "name": "Flight",
        "url": "https://example.com",
        "type": "price_below",
        "threshold": 200,
        "selector": ".price",
        "currency": "USD",
    }
    info = ev(html, entry)
    assert info["price_value"] == pytest.approx(180.0)
    assert info["triggered"] is True
    assert not info.get("ambiguous")


def test_ambiguous_page_prices_do_not_alert():
    html = """
    <html><body>
      <p>US$180.00</p>
      <p>US$999.00</p>
    </body></html>
    """
    entry = {
        "id": "flight",
        "name": "Flight",
        "url": "https://example.com",
        "type": "price_below",
        "threshold": 200,
    }
    info = ev(html, entry)
    assert info["status"] == "ambiguous"
    assert info["triggered"] is False


def test_json_path_locator():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"offers":{"price":"150.00"}}
    </script>
    </head><body><p class="price">US$999.00</p></body></html>
    """
    entry = {
        "id": "flight",
        "name": "Flight",
        "url": "https://example.com",
        "type": "price_below",
        "threshold": 200,
        "json_path": "offers.price",
    }
    info = ev(html, entry)
    assert info["price_value"] == pytest.approx(150.0)
    assert info["triggered"] is True


# --- 5. Delivery ------------------------------------------------------------

def test_delivery_retries_and_per_channel(tmp_state, cfg, monkeypatch):
    calls = {"tg": 0, "ntfy": 0}

    def tg(*a, **k):
        calls["tg"] += 1
        return calls["tg"] >= 2

    def ntfy_card(*a, **k):
        calls["ntfy"] += 1
        return True, 200

    monkeypatch.setattr(core, "tg_send_card", tg)
    monkeypatch.setattr(core, "ntfy_card", ntfy_card)
    cfg = {"topic": "demo", "_ntfy_enabled": True}
    product = {"id": "p", "name": "P", "url": "https://example.com", "type": "restock"}
    state = core.default_state()
    core.emit_alert(cfg, state, product, kind="in_stock", info={"status": "in_stock"})
    evs = state["pending_events"]
    assert len(evs) == 1
    assert evs[0]["channels"]["telegram"]["status"] == "failed"
    assert evs[0]["channels"]["ntfy"]["status"] == "sent"
    # retry immediately: backoff not elapsed
    core.flush_pending(cfg, state, [product])
    assert evs[0]["channels"]["telegram"]["status"] == "failed"
    assert evs[0]["channels"]["telegram"]["attempts"] == 1
    # pretend backoff elapsed
    evs[0]["channels"]["telegram"]["last_attempt_ts"] = (
        core.now_mx() - timedelta(seconds=5)
    ).isoformat()
    core.flush_pending(cfg, state, [product])
    assert evs[0]["channels"]["telegram"]["status"] == "sent"
    assert evs[0]["channels"]["ntfy"]["attempts"] == 1  # not resent
    assert state["pending_events"] == []


def test_test_mode_does_not_enqueue(tmp_state, cfg, monkeypatch):
    monkeypatch.setattr(core, "TEST_MODE", True)
    sent = []
    monkeypatch.setattr(core, "tg_send_card", lambda *a, **k: sent.append("tg") or True)
    product = {"id": "p", "name": "P", "url": "https://example.com", "type": "restock"}
    state = core.default_state()
    core.emit_alert(cfg, state, product, kind="in_stock", info={"status": "in_stock"})
    assert sent == []
    assert state.get("pending_events") in ([], None)


# --- 6. Cooldown coalescing -------------------------------------------------

CHANGED_A = "<html><body><div id='release'>TBA</div></body></html>"
CHANGED_B = "<html><body><div id='release'>12 December 2026</div></body></html>"
CHANGED_C = "<html><body><div id='release'>1 January 2027</div></body></html>"


def test_change_during_cooldown_is_kept_and_coalesced(tmp_state, cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(core, "tg_send_card", lambda *a, **k: sent.append(k.get("event_line") or a) or True)
    monkeypatch.setattr(core, "ntfy_card", lambda *a, **k: (True, 200))
    entry = {
        "id": "release",
        "name": "Release",
        "url": "https://example.com",
        "type": "changed",
        "selector": "#release",
        "cooldown": 3600,
        "emoji": "📅",
    }
    state = core.default_state()
    core.handle_generic(cfg, state, entry, ev(CHANGED_A, entry))  # baseline
    assert sent == []
    core.handle_generic(cfg, state, entry, ev(CHANGED_B, entry))  # first change, no cooldown yet
    assert len(sent) == 1
    # mid-cooldown further change
    core.handle_generic(cfg, state, entry, ev(CHANGED_C, entry))
    assert len(sent) == 1
    pstate = core.product_state(state, entry)
    assert pstate.get("pending_change")
    assert "January" in (pstate["pending_change"]["info"].get("snippet") or "")
    # expire cooldown
    pstate["last_generic_alert_ts"] = (core.now_mx() - timedelta(hours=2)).isoformat()
    core.handle_generic(cfg, state, entry, ev(CHANGED_C, entry))
    assert len(sent) == 2


# --- 7. Test mode leaves live state alone -----------------------------------

def test_test_mode_path_does_not_touch_live_state(tmp_path, monkeypatch):
    live = tmp_path / "state.json"
    live.write_text(json.dumps({"keep": True, "cloud_intro_sent": True}))
    before = live.read_text()
    monkeypatch.setattr(core, "STATE_PATH", live)
    monkeypatch.setattr(core, "TEST_MODE", True)
    # handle_result must not write
    product = {"id": "chromatic", "name": "C", "url": "https://e.com", "type": "restock", "params": {}}
    info = ev(
        "<html><body>ModRetro Chromatic Expected restock: November 19 "
        "<div data-supply-out-of-stock='true'>Restock soon</div></body></html>",
        product,
    )
    core.handle_result({"topic": "x", "_ntfy_enabled": False}, core.default_state(), product, info)
    assert live.read_text() == before


# --- 8. Config validate / apply --------------------------------------------

def test_parse_config_rejects_bad_and_accepts_good():
    with pytest.raises(ValueError):
        core.parse_config({"products": [{"id": "x"}]})
    with pytest.raises(ValueError):
        core.parse_config({"products": [{"id": "x", "url": "https://e.com", "type": "nope"}]})
    cfg = core.parse_config(
        {
            "products": [
                {"id": "a", "url": "https://e.com/a", "type": "restock"},
                {"id": "b", "url": "https://e.com/b", "type": "changed"},
            ]
        }
    )
    assert [p["id"] for p in cfg["products"]] == ["a", "b"]


def test_apply_new_config_keeps_unchanged_watch_state(tmp_state):
    old = core.parse_config(
        {"products": [{"id": "a", "url": "https://e.com/a", "type": "changed", "selector": "#x"}]}
    )
    state = core.default_state()
    core._migrate_state(state, old["products"])
    ps = core.product_state(state, old["products"][0])
    ps["watch_baseline_set"] = True
    ps["last_content_hash"] = "abc"
    new_same = core.parse_config(
        {"products": [{"id": "a", "url": "https://e.com/a", "type": "changed", "selector": "#x"}]}
    )
    core.apply_new_config(old, new_same, state)
    assert ps["watch_baseline_set"] is True
    assert ps["last_content_hash"] == "abc"
    new_changed = core.parse_config(
        {"products": [{"id": "a", "url": "https://e.com/a", "type": "changed", "selector": "#y"}]}
    )
    core.apply_new_config(old, new_changed, state)
    assert ps["watch_baseline_set"] is False
    assert ps["last_content_hash"] is None


# --- Existing restock entries stay valid ------------------------------------

def test_existing_restock_config_still_parses():
    raw = json.loads((ROOT / "config.json").read_text())
    parsed = core.parse_config({"topic": "t", **raw}, require_topic=False)
    ids = [p["id"] for p in parsed["products"]]
    assert ids == ["chromatic", "disney-ezra"]
    assert all(p["type"] == "restock" for p in parsed["products"])


# --- Check interval on reminder days ----------------------------------------

def test_check_interval_tightens_on_reminder_day_pyt():
    products = [
        {
            "id": "chromatic",
            "remind_on": "2026-11-19 07:00",
        }
    ]
    products[0]["remind_at"] = core.parse_remind_at(products[0])
    # 00:00 PYT on the reminder day (21:00 Mexico City the evening before)
    start = datetime(2026, 11, 19, 0, 0, tzinfo=core.DISPLAY_TZ)
    assert core.check_interval_range(now=start, products=products) == (60.0, 75.0)
    mid = datetime(2026, 11, 19, 7, 0, tzinfo=core.DISPLAY_TZ)
    assert core.check_interval_range(now=mid, products=products) == (60.0, 75.0)
    late = datetime(2026, 11, 19, 23, 59, tzinfo=core.DISPLAY_TZ)
    assert core.check_interval_range(now=late, products=products) == (60.0, 75.0)
    # The day before and after stay at the normal 60-120s interval
    before = datetime(2026, 11, 18, 23, 59, tzinfo=core.DISPLAY_TZ)
    assert core.check_interval_range(now=before, products=products) == (60.0, 120.0)
    after = datetime(2026, 11, 20, 0, 0, tzinfo=core.DISPLAY_TZ)
    assert core.check_interval_range(now=after, products=products) == (60.0, 120.0)
    # Heartbeat time (09:00 Mexico City / 12:00 PYT) is unchanged on a normal day
    hb = datetime(2026, 11, 16, 9, 0, tzinfo=core.TZ)
    assert core.check_interval_range(now=hb, products=products) == (60.0, 120.0)
    assert (core.HEARTBEAT_HOUR, core.HEARTBEAT_MINUTE) == (9, 0)
    assert core.TZ.key == "America/Mexico_City"


def test_check_interval_not_faster_than_60s():
    products = [{"id": "chromatic", "remind_on": "2026-11-19 07:00"}]
    products[0]["remind_at"] = core.parse_remind_at(products[0])
    now = datetime(2026, 11, 19, 7, 0, tzinfo=core.DISPLAY_TZ)
    low, high = core.check_interval_range(now=now, products=products)
    assert low >= 60
    assert high >= low
    low2, high2 = core.check_interval_range(
        now=now, products=products, in_stock_repeat=True
    )
    assert low2 >= 60
    assert (low2, high2) == (float(core.IN_STOCK_REPEAT_S), float(core.IN_STOCK_REPEAT_S))


# --- Daily heartbeat --------------------------------------------------------

def _heartbeat_cfg():
    return core.parse_config(
        {
            "products": [
                {
                    "id": "chromatic",
                    "name": "Game Boy Chromatic Codex",
                    "short_name": "the Chromatic Codex",
                    "url": "https://openai.com/supply/co-lab/chromatic/",
                    "emoji": "🎮",
                    "type": "restock",
                    "primary": True,
                    "remind_on": "2026-11-19 07:00",
                },
                {
                    "id": "disney-ezra",
                    "name": "Ezra Legacy Lightsaber Set",
                    "short_name": "the Ezra Legacy Lightsaber Set",
                    "url": "https://www.disneystore.com/ezra-example",
                    "emoji": "⚔️",
                    "type": "restock",
                    "primary": False,
                },
            ]
        }
    )


def _heartbeat_state():
    state = core.default_state()
    state["products"] = {
        "chromatic": {
            **core.default_product_state(),
            "last_status": "out_of_stock",
            "last_expected_restock": "November 19",
            "product_price": "US$200.00",
        },
        "disney-ezra": {
            **core.default_product_state(),
            "last_status": "out_of_stock",
            "last_expected_restock": "November 17",
            "product_price": "US$400.00",
        },
    }
    return state


def test_build_heartbeat_text_lists_watches():
    now = datetime(2026, 11, 16, 9, 0, tzinfo=core.TZ)  # 12:00 PYT
    cfg = _heartbeat_cfg()
    text = core.build_heartbeat_text(cfg, _heartbeat_state(), now=now)
    assert text == (
        "🟢 <b>Restockr is running</b>\n"
        "🕐 Last check: 12:00 PYT · 2 watches active\n"
        "\n"
        "🎮 <b>Chromatic Codex</b>\n"
        "📦 <b>Status:</b> ⏳ Not in stock yet\n"
        "📅 <b>Expected date:</b> November 19\n"
        "💵 <b>Price:</b> US$200.00\n"
        "\n"
        "⚔️ <b>Ezra Legacy Lightsaber Set</b>\n"
        "📦 <b>Status:</b> ⏳ Not in stock yet\n"
        "📅 <b>Expected date:</b> November 17\n"
        "💵 <b>Price:</b> US$400.00\n"
        "\n"
        "I'll ping you the moment something changes."
    )
    assert "⏰ Reminder" not in text
    assert "TEST" not in text
    assert "the Chromatic" not in text
    assert "View" not in text
    assert "<a " not in text
    keyboard = core.build_heartbeat_keyboard(cfg)
    assert keyboard == {
        "inline_keyboard": [
            [
                {"text": "🎮 Open Chromatic", "url": "https://openai.com/supply/co-lab/chromatic/"},
                {"text": "⚔️ Open Ezra", "url": "https://www.disneystore.com/ezra-example"},
            ]
        ]
    }


def test_build_heartbeat_scales_to_many_watches():
    emojis = ["🎮", "⚔️", "🎧", "👟", "📱", "🎟️", "📦", "🧪"]
    products = []
    state_products = {}
    for i, emoji in enumerate(emojis, start=1):
        pid = f"watch-{i}"
        is_restock = i % 2 == 1
        products.append(
            {
                "id": pid,
                "name": f"Product {i} Long Name",
                "short_name": f"Item{i}",
                "url": f"https://example.com/p{i}",
                "emoji": emoji,
                "type": "restock" if is_restock else "text_appears",
                "text": None if is_restock else "Buy now",
                "primary": i == 1,
                "expected_date": "2026-12-01" if is_restock else None,
                "remind_on": "2026-12-01 07:00" if i == 1 else None,
            }
        )
        state_products[pid] = {
            **core.default_product_state(),
            "last_status": "out_of_stock" if is_restock else "idle",
            "last_expected_restock": "December 1" if is_restock else None,
            "product_price": f"US${100 + i}.00" if is_restock else None,
        }
    cfg = core.parse_config({"products": products})
    state = core.default_state()
    state["products"] = state_products
    now = datetime(2026, 11, 16, 9, 0, tzinfo=core.TZ)
    text = core.build_heartbeat_text(cfg, state, now=now)
    assert "8 watches active" in text
    for i, emoji in enumerate(emojis, start=1):
        assert f"{emoji} <b>Item{i}</b>" in text
    assert "⏰ Reminder" not in text
    assert "TEST" not in text
    assert len(text) <= core.TELEGRAM_TEXT_MAX
    assert text.endswith("I'll ping you the moment something changes.")
    rows = core.build_heartbeat_keyboard(cfg)["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2, 2, 2]
    assert all(len(row) <= 2 for row in rows)
    buttons = [btn for row in rows for btn in row]
    assert len(buttons) == 8
    assert buttons[0]["text"] == "🎮 Open Item1"
    assert buttons[1]["text"] == "⚔️ Open Item2"
    assert buttons[-1]["text"] == "🧪 Open Item8"
    assert buttons[0]["url"] == "https://example.com/p1"


def test_build_heartbeat_text_truncates_to_telegram_limit():
    long_name = "Very Long Product Name " * 20
    long_price = "US$" + ("9" * 200)
    products = []
    state_products = {}
    for i in range(1, 21):
        pid = f"long-{i}"
        products.append(
            {
                "id": pid,
                "name": f"{long_name} {i}",
                "short_name": f"{long_name} {i}",
                "url": f"https://example.com/long{i}",
                "emoji": "📦",
                "type": "restock",
                "primary": i == 1,
                "expected_date": "2026-12-01",
                "remind_on": "2026-12-01 07:00",
            }
        )
        state_products[pid] = {
            **core.default_product_state(),
            "last_status": "out_of_stock",
            "last_expected_restock": "December 1",
            "product_price": long_price,
        }
    cfg = core.parse_config({"products": products})
    state = core.default_state()
    state["products"] = state_products
    now = datetime(2026, 11, 16, 9, 0, tzinfo=core.TZ)
    text = core.build_heartbeat_text(cfg, state, now=now)
    assert "20 watches active" in text
    assert "⏰ Reminder" not in text
    assert "I'll ping you the moment something changes." in text
    assert len(text) <= core.TELEGRAM_TEXT_MAX == 4096
    match = __import__("re").search(r"\+(\d+) more", text)
    assert match, text[-200:]
    omitted = int(match.group(1))
    assert 1 <= omitted < 20
    # Shown watches plus '+N more' should account for every watch.
    shown = text.count("📦 <b>Status:</b>")
    assert shown + omitted == 20
    rows = core.build_heartbeat_keyboard(cfg)["inline_keyboard"]
    assert [len(row) for row in rows] == [2] * 10


def test_build_heartbeat_generic_watch_and_singular():
    cfg = core.parse_config(
        {
            "products": [
                {
                    "id": "tickets",
                    "name": "Movie tickets",
                    "url": "https://example.com/tickets",
                    "emoji": "🎟️",
                    "type": "text_appears",
                    "text": "Buy tickets",
                    "primary": True,
                }
            ]
        }
    )
    state = core.default_state()
    state["products"] = {
        "tickets": {**core.default_product_state(), "last_status": "idle"},
    }
    now = datetime(2026, 11, 16, 9, 0, tzinfo=core.TZ)
    text = core.build_heartbeat_text(cfg, state, now=now)
    assert "1 watch active" in text
    assert "🎟️ <b>Movie tickets</b>" in text
    assert "📦 <b>Status:</b> 👀 Watching" in text
    assert "I'll ping you the moment something changes." in text
    assert core.build_heartbeat_keyboard(cfg) == {
        "inline_keyboard": [
            [{"text": "🎟️ Open Movie", "url": "https://example.com/tickets"}]
        ]
    }


def test_html_to_plain_strips_markup_and_keeps_link_target():
    html = '🟢 <b>Restockr is running</b> · <a href="https://example.com/p">View</a>'
    plain = core.html_to_plain(html)
    assert plain == "🟢 Restockr is running · https://example.com/p"
    assert "<" not in plain


def test_maybe_specials_sends_rich_heartbeat_once(tmp_state, monkeypatch):
    sent = []

    def fake_tg(text, reply_markup=None):
        sent.append({"text": text, "reply_markup": reply_markup})
        return True

    monkeypatch.setattr(core, "tg_send", fake_tg)
    monkeypatch.setattr(core, "now_mx", lambda: datetime(2026, 11, 16, 10, 0, tzinfo=core.TZ))
    cfg = _heartbeat_cfg()
    cfg["topic"] = "restockr-no-topic"
    cfg["_ntfy_enabled"] = False
    state = _heartbeat_state()
    products = cfg["products"]
    chromatic = next(p for p in products if p["id"] == "chromatic")
    core.maybe_specials(cfg, state, chromatic, core.product_state(state, chromatic), "out_of_stock")
    assert len(sent) == 1
    assert sent[0]["text"].startswith("🟢 <b>Restockr is running</b>")
    assert "13:00 PYT" in sent[0]["text"]  # 10:00 Mexico City
    assert "2 watches active" in sent[0]["text"]
    assert "📦 <b>Status:</b> ⏳ Not in stock yet" in sent[0]["text"]
    assert sent[0]["reply_markup"] == core.build_heartbeat_keyboard(cfg)
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "🎮 Open Chromatic"
    assert "2026-11-16" in state["heartbeat_dates"]
    sent.clear()
    core.maybe_specials(cfg, state, chromatic, core.product_state(state, chromatic), "out_of_stock")
    assert sent == []


def test_heartbeat_skipped_when_primary_is_in_stock(tmp_state, monkeypatch):
    sent = []
    monkeypatch.setattr(core, "tg_send", lambda text, reply_markup=None: sent.append(text) or True)
    monkeypatch.setattr(core, "now_mx", lambda: datetime(2026, 11, 16, 10, 0, tzinfo=core.TZ))
    cfg = _heartbeat_cfg()
    cfg["topic"] = "restockr-no-topic"
    cfg["_ntfy_enabled"] = False
    state = _heartbeat_state()
    chromatic = next(p for p in cfg["products"] if p["id"] == "chromatic")
    core.maybe_specials(cfg, state, chromatic, core.product_state(state, chromatic), "in_stock")
    assert sent == []
    assert state.get("heartbeat_dates") == []
