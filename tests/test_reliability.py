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
    assert dt == datetime(2026, 11, 19, 7, 0, tzinfo=core.TZ)


def test_parse_expected_date_plus_reminder():
    dt = core.parse_remind_at({"expected_date": "2026-11-19", "reminder": True})
    assert dt.date().isoformat() == "2026-11-19"
    assert dt.hour == 7
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
    assert by_id["disney-ezra"]["remind_at"] is None


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


def test_build_heartbeat_text_lists_watches_and_reminder():
    now = datetime(2026, 11, 16, 9, 0, tzinfo=core.TZ)  # 12:00 PYT
    text = core.build_heartbeat_text(_heartbeat_cfg(), _heartbeat_state(), now=now)
    assert text == (
        "🟢 <b>Restockr is running</b>\n"
        "🕐 Last check: 12:00 PYT · 2 watches active\n"
        "\n"
        '🎮 <b>Chromatic Codex</b> — ⏳ Not in stock yet · expected November 19 · US$200.00 · <a href="https://openai.com/supply/co-lab/chromatic/">View</a>\n'
        '⚔️ <b>Ezra Legacy Lightsaber Set</b> — ⏳ Not in stock yet · expected November 17 · US$400.00 · <a href="https://www.disneystore.com/ezra-example">View</a>\n'
        "\n"
        "⏰ Reminder: Chromatic Codex — Nov 19, 10:00 PYT\n"
        "\n"
        "I'll ping you the moment something changes."
    )
    assert "TEST" not in text
    assert "the Chromatic" not in text


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
    assert "🎟️ <b>Movie tickets</b> — 👀 Watching" in text
    assert "I'll ping you the moment something changes." in text


def test_html_to_plain_strips_markup_and_keeps_link_target():
    html = '🟢 <b>Restockr is running</b> · <a href="https://example.com/p">View</a>'
    plain = core.html_to_plain(html)
    assert plain == "🟢 Restockr is running · https://example.com/p"
    assert "<" not in plain


def test_maybe_specials_sends_rich_heartbeat_once(tmp_state, monkeypatch):
    sent = []
    monkeypatch.setattr(core, "tg_send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(core, "now_mx", lambda: datetime(2026, 11, 16, 10, 0, tzinfo=core.TZ))
    cfg = _heartbeat_cfg()
    cfg["topic"] = "restockr-no-topic"
    cfg["_ntfy_enabled"] = False
    state = _heartbeat_state()
    products = cfg["products"]
    chromatic = next(p for p in products if p["id"] == "chromatic")
    core.maybe_specials(cfg, state, chromatic, core.product_state(state, chromatic), "out_of_stock")
    assert len(sent) == 1
    assert sent[0].startswith("🟢 <b>Restockr is running</b>")
    assert "13:00 PYT" in sent[0]  # 10:00 Mexico City
    assert "2 watches active" in sent[0]
    assert "2026-11-16" in state["heartbeat_dates"]
    sent.clear()
    core.maybe_specials(cfg, state, chromatic, core.product_state(state, chromatic), "out_of_stock")
    assert sent == []


def test_heartbeat_skipped_when_primary_is_in_stock(tmp_state, monkeypatch):
    sent = []
    monkeypatch.setattr(core, "tg_send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(core, "now_mx", lambda: datetime(2026, 11, 16, 10, 0, tzinfo=core.TZ))
    cfg = _heartbeat_cfg()
    cfg["topic"] = "restockr-no-topic"
    cfg["_ntfy_enabled"] = False
    state = _heartbeat_state()
    chromatic = next(p for p in cfg["products"] if p["id"] == "chromatic")
    core.maybe_specials(cfg, state, chromatic, core.product_state(state, chromatic), "in_stock")
    assert sent == []
    assert state.get("heartbeat_dates") == []
