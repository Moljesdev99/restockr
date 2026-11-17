"""Waiting room (Queue-it) transitions and Disney stock detection. No network."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import restockr_core as core

EZRA = {
    "id": "disney-ezra",
    "name": "Ezra Legacy Lightsaber Set",
    "short_name": "the Ezra Legacy Lightsaber Set",
    "url": "https://www.disneystore.com/ezra-legacy-lightsaber-set-star-wars-rebels-limited-edition-418149061750.html",
    "emoji": "⚔️",
}

# Trimmed from the live page on 2026-11-17 after the drop (in stock). Note the
# hidden "product-oos-info d-none" template and data-soldOut-copy attribute:
# both are always present and must not count as out of stock.
_BASE = """<html><head><title>Ezra Legacy LIGHTSABER Set | Disney Store</title></head><body>
<div class="product-detail" data-product="{{&quot;availability&quot;:&quot;online - {avail}&quot;}}">
<h1>Ezra Legacy LIGHTSABER Set</h1>
<span class="sales">US$400.00</span>
<div class="{oos_cls}"><div class="product-oos-info-title">Out of Stock</div>
<div class="product-oos-info-message">We&rsquo;re sorry, this product is currently out of stock.</div></div>
<button type="submit" class="sign-in-purchase button--primary add-to-bag-button" id="add-to-cart"
 data-pid="418149061750" data-soldOut-copy="Sold Out" {disabled}><span>{label}</span></button>
<div itemprop="availability">http://schema.org/{schema}</div>
<div>disneystore</div></div></body></html>"""


def page(avail="in_stock", oos_cls="product-oos-info d-none", disabled="", label="Log In &amp; Purchase", schema="InStock"):
    return _BASE.format(avail=avail, oos_cls=oos_cls, disabled=disabled, label=label, schema=schema)


def ev(html, entry=EZRA):
    return core.evaluate_page(core._title_from_html(html), html, core._visible_from_html(html), entry)


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(core, "TEST_MODE", False)


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, str | None, str | None]] = []

    def fake_emit(cfg, state, product, **kw):
        out.append((kw.get("kind"), (kw.get("info") or {}).get("status"), kw.get("event_line")))
        return {"kind": kw.get("kind")}

    monkeypatch.setattr(core, "emit_alert", fake_emit)
    return out


CFG = {"topic": "restockr-no-topic", "_ntfy_enabled": False}


# --- Disney stock detection --------------------------------------------------

def test_disney_in_stock_ignores_hidden_oos_template():
    info = ev(page())
    assert info["status"] == "in_stock"
    assert info["enabled_add"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"disabled": "disabled", "label": "Coming Soon"},
        {"label": "Sold Out"},
        {"oos_cls": "product-oos-info"},
        {"avail": "out_of_stock", "schema": "OutOfStock", "disabled": "disabled"},
    ],
)
def test_disney_out_of_stock_variants(kwargs):
    assert ev(page(**kwargs))["status"] == "out_of_stock"


def test_live_snapshot_if_present():
    snap = Path("/tmp/ezra.html")
    if not snap.exists():
        pytest.skip("no live snapshot")
    assert core.disney_html_stock(snap.read_text()) == "in_stock"  # snapshot taken while in stock


def test_variation_json():
    assert core.disney_variation_status({"product": {"available": True, "readyToOrder": True}}) == "in_stock"
    assert core.disney_variation_status({"product": {"available": False, "readyToOrder": False}}) == "out_of_stock"
    assert core.disney_variation_status({"product": {"available": True, "isForceSellOutProduct": True}}) == "out_of_stock"
    assert core.disney_variation_status({"x": 1}) is None
    assert core.disney_variation_url(EZRA).endswith("Product-Variation?pid=418149061750&quantity=1")


def test_apply_variation_overrides_and_detects_queue():
    oos = {"status": "out_of_stock", "product_ok": True}
    fetch_ok = lambda u: (u, '{"product": {"available": true, "readyToOrder": true}}')
    assert core.apply_disney_variation(EZRA, oos, fetch_ok)["status"] == "in_stock"
    fetch_q = lambda u: ("https://queue.disneystore.com/?c=disney&e=drop", "<html>queue-it</html>")
    assert core.apply_disney_variation(EZRA, oos, fetch_q)["status"] == "queue"

    def boom(u):
        raise RuntimeError("blocked")

    assert core.apply_disney_variation(EZRA, oos, boom)["status"] == "out_of_stock"


def test_looks_like_queue():
    assert core.looks_like_queue("", "", "https://queue.disneystore.com/?c=x")
    assert core.looks_like_queue("Virtual Queue", "<html><script src='//static.queue-it.net/x.js'></script></html>")
    assert not core.looks_like_queue("", page(), EZRA["url"])


# --- Transitions -------------------------------------------------------------

def run(state, info):
    core.handle_check(CFG, state, EZRA, info)


def test_full_drop_sequence_alerts_each_transition_once(tmp_state, sent, monkeypatch):
    state = core.default_state()
    clock = [1_000_000.0]
    monkeypatch.setattr(core.time, "time", lambda: clock[0])
    oos = ev(page(disabled="disabled", label="Coming Soon"))

    run(state, oos)
    assert sent == []

    for _ in range(10):  # 10 queue reads, 1 min apart -> 3 open alerts max
        run(state, {"status": "queue", "product_ok": True})
        clock[0] += 60
    assert [k for k, *_ in sent] == ["queue"] * 3
    assert sent[0][2] == core.QUEUE_OPEN_LINE
    sent.clear()

    run(state, oos)  # first reachable read: not confirmed yet
    assert sent == []
    run(state, oos)  # confirmed -> one "over" alert
    assert [k for k, *_ in sent] == ["queue_ended"]
    assert sent[0][2] == core.QUEUE_ENDED_LINE
    sent.clear()
    run(state, oos)
    assert sent == []

    run(state, ev(page()))  # in stock
    assert [k for k, *_ in sent] == ["in_stock"]
    sent.clear()

    run(state, ev(page(label="Sold Out")))
    assert [k for k, *_ in sent] == ["sold_out"]


def test_queue_straight_to_in_stock_sends_over_then_in_stock(tmp_state, sent):
    state = core.default_state()
    run(state, {"status": "queue", "product_ok": True})
    sent.clear()
    run(state, ev(page()))
    assert [k for k, *_ in sent] == ["queue_ended", "in_stock"]
    assert core.product_state(state, EZRA)["queue_active"] is False


def test_queue_flap_does_not_end_or_resend(tmp_state, sent, monkeypatch):
    state = core.default_state()
    clock = [5_000_000.0]
    monkeypatch.setattr(core.time, "time", lambda: clock[0])
    oos = ev(page(disabled="disabled", label="Coming Soon"))
    run(state, {"status": "queue", "product_ok": True})
    clock[0] += 60
    run(state, oos)  # single flaky reachable read
    clock[0] += 60
    run(state, {"status": "queue", "product_ok": True})
    assert [k for k, *_ in sent] == ["queue"]  # still the same waiting room


def test_queue_state_survives_restart(tmp_state, sent):
    state = core.default_state()
    run(state, {"status": "queue", "product_ok": True})
    state2 = core.load_state([EZRA])
    sent.clear()
    run(state2, ev(page()))
    assert [k for k, *_ in sent] == ["queue_ended", "in_stock"]


# --- Card format -------------------------------------------------------------

def test_cards():
    instock = ev(page())
    card = core.build_caption(instock, {}, product=EZRA)
    first = card.splitlines()[0]
    assert first == "⚔️ <b>Ezra Legacy LIGHTSABER Set</b>".replace("LIGHTSABER", "Lightsaber")
    assert "🟢 In stock, buy now!" in card
    assert "Expected date" not in card

    over = core.build_caption(instock, {}, product=EZRA, event_line=core.QUEUE_ENDED_LINE)
    assert over.splitlines()[0] == core.QUEUE_ENDED_LINE
    assert "🟢 In stock, buy now!" in over

    q = core.build_caption({"status": "queue"}, {}, product=EZRA, event_line=core.QUEUE_OPEN_LINE)
    assert q.splitlines()[0] == core.QUEUE_OPEN_LINE
    assert "Waiting room open" in q
    assert core.button_label(EZRA) == "🛒 View product"
