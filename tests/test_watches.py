#!/usr/bin/env python3
"""Unit and dry-run tests for Restockr watch types. Does not send alerts."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import restockr_core as core  # noqa: E402


# --- Simulated pages --------------------------------------------------------

SUPPLY_OOS = """
<html><head>
<title>ModRetro Chromatic — Codex Edition</title>
<meta property="og:description" content="Create your own games with Codex, load them onto a cartridge, and play on a handheld">
<meta property="og:image" content="https://example.com/chroma.png">
</head><body>
<h1>ModRetro Chromatic</h1>
<div data-supply-out-of-stock="true">Restock soon</div>
<p>Expected restock: November 19</p>
<button class="add-to-cart-release" disabled>Coming Soon</button>
<div role="status">$200.00</div>
</body></html>
"""

SUPPLY_IN_STOCK = """
<html><head>
<title>ModRetro Chromatic — Codex Edition</title>
<meta property="og:description" content="Create your own games with Codex, load them onto a cartridge, and play on a handheld">
</head><body>
<h1>ModRetro Chromatic</h1>
<button data-analytics="supply-add-to-cart">Add to cart</button>
<div role="status">$200.00</div>
</body></html>
"""

DISNEY_OOS = """
<html><head>
<title>Ezra Legacy Lightsaber Set | shopDisney</title>
<meta property="og:description" content="Limited edition Ezra lightsaber hilt set featuring two Legacy hilts.">
</head><body>
<div class="disneystore">
<h1 class="product-name">Ezra Legacy Lightsaber Set</h1>
<p>Currently out of stock</p>
<p>Available Nov 17 at 8am PT</p>
<button class="add-to-cart-release" disabled>Coming Soon</button>
<div class="value" content="400.00">$400.00</div>
</div>
</body></html>
"""

TICKETS_HTML = """
<html><head><title>Premiere tickets</title></head>
<body>
<h1>The Movie</h1>
<p id="status">Buy tickets — now on sale</p>
</body></html>
"""

SOLD_OUT_HTML = """
<html><head><title>Hotel night</title></head>
<body>
<div class="availability">Sold out</div>
</body></html>
"""

PRICE_HTML = """
<html><head>
<title>Flight offer</title>
<script type="application/ld+json">
{"@type":"Offer","price":"180.00","priceCurrency":"USD"}
</script>
</head>
<body>
<p class="price">US$180.00</p>
</body></html>
"""

CHANGED_A = """<html><body><div id="release">Release date: To be announced</div></body></html>"""
CHANGED_B = """<html><body><div id="release">Release date: 12 December 2026</div></body></html>"""


def ev(html: str, entry: dict) -> dict:
    title = core._title_from_html(html)
    visible = core._visible_from_html(html)
    return core.evaluate_page(title, html, visible, entry)


def restock_entry(**kw) -> dict:
    base = {
        "id": "chromatic",
        "name": "Game Boy Chromatic Codex",
        "short_name": "the Chromatic Codex",
        "url": "https://openai.com/supply/co-lab/chromatic/",
        "emoji": "🎮",
        "type": "restock",
        "params": {},
    }
    base.update(kw)
    return base


class TestEnglishMessages(unittest.TestCase):
    def test_status_and_captions_are_english(self):
        self.assertEqual(core.status_label("in_stock", None), "In stock")
        self.assertEqual(core.status_label("out_of_stock", "November 19"), "Coming soon")
        self.assertEqual(core.status_label("out_of_stock", None), "Sold out")
        self.assertEqual(core.format_date("November 19"), "November 19")
        self.assertEqual(core.format_date("Nov 17"), "November 17")
        info = ev(SUPPLY_IN_STOCK, restock_entry())
        cap = core.build_caption(info, {}, product=restock_entry())
        self.assertIn("📦", cap)
        self.assertIn("🟢 In stock, buy now!", cap)
        self.assertNotIn("is in stock!", cap)
        self.assertNotIn("Expected date:", cap)
        self.assertTrue(cap.strip().startswith("🎮"))
        self.assertIn("Status:", cap)
        self.assertIn("Price:", cap)
        self.assertNotRegex(cap, r"[ÁÉÍÓÚáéíóúñÑ¡¿]")
        # Coming-soon card still shows the expected date and has no buy-now status
        oos = ev(SUPPLY_OOS, restock_entry())
        oos_cap = core.build_caption(oos, {}, product=restock_entry())
        self.assertIn("Coming soon", oos_cap)
        self.assertIn("Expected date:", oos_cap)
        self.assertNotIn("In stock, buy now!", oos_cap)
        md = core.build_caption(info, {}, product=restock_entry(), markup="md")
        self.assertIn("**Status:**", md)
        self.assertIn("🟢 In stock, buy now!", md)
        self.assertNotIn("is in stock!", md)
        self.assertEqual(
            core.card_headline(restock_entry(), info),
            "🎮 Game Boy Chromatic Codex",
        )
        self.assertEqual(core.button_label(restock_entry()), "🛒 View product")
        self.assertEqual(
            core.button_label({"id": "x", "url": "https://e.com", "type": "changed"}),
            "🔗 Open page",
        )


class TestRestockRegression(unittest.TestCase):
    def test_supply_out_of_stock(self):
        info = ev(SUPPLY_OOS, restock_entry())
        self.assertTrue(info["product_ok"])
        self.assertEqual(info["status"], "out_of_stock")
        self.assertEqual(info["expected"], "November 19")
        self.assertEqual(info["price"], "US$200.00")

    def test_supply_in_stock(self):
        info = ev(SUPPLY_IN_STOCK, restock_entry())
        self.assertEqual(info["status"], "in_stock")

    def test_disney_out_of_stock(self):
        entry = restock_entry(
            id="disney-ezra",
            name="Ezra Legacy Lightsaber Set",
            url="https://www.disneystore.com/ezra-legacy-lightsaber-set-star-wars-rebels-limited-edition-418149061750.html",
        )
        info = ev(DISNEY_OOS, entry)
        self.assertTrue(info["product_ok"])
        self.assertEqual(info["status"], "out_of_stock")
        self.assertEqual(info["expected"], "November 17")

    def test_live_config_defaults_to_restock(self):
        raw = json.loads((ROOT / "config.json").read_text())
        ids = [p["id"] for p in raw["products"]]
        self.assertEqual(ids, ["chromatic", "chromatic-case", "disney-ezra"])
        self.assertTrue(all(not p.get("type") or p.get("type") in ("restock", "product") for p in raw["products"]))


class TestTextAppears(unittest.TestCase):
    def test_present_and_absent(self):
        entry = {
            "id": "tickets",
            "name": "Movie tickets",
            "url": "https://example.com/tickets",
            "type": "text_appears",
            "text": "Buy tickets",
        }
        on = ev(TICKETS_HTML, entry)
        self.assertTrue(on["triggered"])
        self.assertEqual(on["status"], "triggered")
        off = ev(SOLD_OUT_HTML, entry)
        self.assertFalse(off["triggered"])
        self.assertEqual(off["status"], "idle")

    def test_selector_and_regex(self):
        entry = {
            "id": "tickets",
            "name": "Movie tickets",
            "url": "https://example.com/tickets",
            "type": "text_appears",
            "selector": "#status",
            "pattern": r"Buy tickets",
        }
        self.assertTrue(ev(TICKETS_HTML, entry)["triggered"])


class TestTextDisappears(unittest.TestCase):
    def test_sold_out_gone(self):
        entry = {
            "id": "hotel",
            "name": "Hotel night",
            "url": "https://example.com/hotel",
            "type": "text_disappears",
            "text": "Sold out",
        }
        still = ev(SOLD_OUT_HTML, entry)
        self.assertFalse(still["triggered"])
        gone = ev("<html><body><div class='availability'>Available</div></body></html>", entry)
        self.assertTrue(gone["triggered"])


class TestPriceBelow(unittest.TestCase):
    def test_jsonld_and_threshold(self):
        entry = {
            "id": "flight",
            "name": "Flight",
            "url": "https://example.com/flight",
            "type": "price_below",
            "threshold": 200,
            "currency": "USD",
        }
        info = ev(PRICE_HTML, entry)
        self.assertEqual(info["price_value"], 180.0)
        self.assertTrue(info["triggered"])
        self.assertIn("US$180.00", info["price"])

        high = dict(entry, threshold=100)
        self.assertFalse(ev(PRICE_HTML, high)["triggered"])

    def test_css_selector_price(self):
        entry = {
            "id": "flight",
            "name": "Flight",
            "url": "https://example.com/flight",
            "type": "price_below",
            "threshold": 200,
            "selector": ".price",
            "jsonld": False,
        }
        info = ev(PRICE_HTML, entry)
        self.assertEqual(info["price_value"], 180.0)
        self.assertTrue(info["triggered"])

    def test_parse_price_formats(self):
        self.assertEqual(core.parse_price("US$200.00"), 200.0)
        self.assertEqual(core.parse_price("£51.77"), 51.77)
        self.assertEqual(core.parse_price("180"), 180.0)


class TestChanged(unittest.TestCase):
    def test_hash_changes(self):
        entry = {
            "id": "release",
            "name": "Release date",
            "url": "https://example.com/release",
            "type": "changed",
            "selector": "#release",
        }
        a = ev(CHANGED_A, entry)
        b = ev(CHANGED_B, entry)
        self.assertTrue(a["content_hash"])
        self.assertTrue(b["content_hash"])
        self.assertNotEqual(a["content_hash"], b["content_hash"])
        self.assertEqual(a["content_hash"], ev(CHANGED_A, entry)["content_hash"])


class TestGenericAlertBaseline(unittest.TestCase):
    def test_first_check_does_not_alert(self):
        entry = {
            "id": "tickets",
            "name": "Movie tickets",
            "url": "https://example.com/tickets",
            "type": "text_appears",
            "text": "Buy tickets",
            "emoji": "🎟️",
            "is_test": False,
        }
        sent = {"n": 0}

        def fake_tg(*_a, **_k):
            sent["n"] += 1
            return True

        def fake_ntfy(*_a, **_k):
            return True, 200

        core.tg_send_card = fake_tg
        core.ntfy_card = fake_ntfy
        core.ntfy = lambda *a, **k: (True, 200)
        core.tg_send = lambda *a, **k: True
        import tempfile
        from pathlib import Path as P
        core.STATE_PATH = P(tempfile.mkstemp(suffix=".json")[1])
        state = core.default_state()
        info = ev(TICKETS_HTML, entry)
        core.handle_generic({"topic": "test"}, state, entry, info)
        self.assertEqual(sent["n"], 0)
        self.assertTrue(state["products"]["tickets"]["watch_baseline_set"])
        # Second check, still present: no transition, no cooldown → no alert
        core.handle_generic({"topic": "test"}, state, entry, ev(TICKETS_HTML, entry))
        self.assertEqual(sent["n"], 0)
        # Reset triggered flag then it appears again
        state["products"]["tickets"]["last_watch_triggered"] = False
        core.handle_generic({"topic": "test"}, state, entry, ev(TICKETS_HTML, entry))
        self.assertEqual(sent["n"], 1)


@pytest.mark.network
class TestLivePages(unittest.TestCase):
    """Fetch a few public pages. Failures are reported honestly, not hidden."""

    def _fetch(self, url: str) -> tuple[str, str, str]:
        from curl_cffi import requests as cr

        r = cr.get(url, timeout=30, impersonate="chrome", allow_redirects=True,
                   headers={"Accept-Language": "en-US,en;q=0.9"})
        r.raise_for_status()
        html = r.text or ""
        return core._title_from_html(html), html, core._visible_from_html(html)

    def test_example_com_text_appears(self):
        title, html, visible = self._fetch("https://example.com/")
        entry = {
            "id": "example",
            "name": "Example Domain",
            "url": "https://example.com/",
            "type": "text_appears",
            "text": "Example Domain",
        }
        info = core.evaluate_page(title, html, visible, entry)
        self.assertTrue(info["triggered"], msg=f"title={title!r} len={len(html)}")
        # Opposite type
        gone = dict(entry, type="text_disappears", text="This string is not on the page")
        # "This string is not on the page" is absent → text_disappears is triggered
        info2 = core.evaluate_page(title, html, visible, gone)
        self.assertTrue(info2["triggered"])

    def test_example_com_changed_stable(self):
        title, html, visible = self._fetch("https://example.com/")
        entry = {
            "id": "example-changed",
            "name": "Example Domain",
            "url": "https://example.com/",
            "type": "changed",
        }
        a = core.evaluate_page(title, html, visible, entry)
        b = core.evaluate_page(title, html, visible, entry)
        self.assertEqual(a["content_hash"], b["content_hash"])

    def test_books_toscrape_price_below(self):
        url = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
        title, html, visible = self._fetch(url)
        entry = {
            "id": "book",
            "name": "A Light in the Attic",
            "url": url,
            "type": "price_below",
            "threshold": 100,
            "selector": "p.price_color",
            "currency": "GBP",
            "jsonld": False,
        }
        info = core.evaluate_page(title, html, visible, entry)
        self.assertIsNotNone(info.get("price_value"), msg=f"title={title!r} len={len(html)}")
        self.assertLess(info["price_value"], 100)
        self.assertTrue(info["triggered"])

    def test_live_chromatic_and_disney_no_regression(self):
        products = json.loads((ROOT / "config.json").read_text())["products"]
        results = {}
        for p in products:
            p = dict(p)
            p["type"] = "restock"
            title, html, visible = self._fetch(p["url"])
            try:
                info = core.evaluate_page(title, html, visible, p)
            except Exception as exc:
                results[p["id"]] = f"ERROR: {exc}"
                continue
            results[p["id"]] = {
                "status": info.get("status"),
                "expected": info.get("expected"),
                "product_ok": info.get("product_ok"),
                "price": info.get("price"),
            }
        print("LIVE RESTOCK RESULTS:", json.dumps(results, indent=2))
        for pid, info in results.items():
            self.assertIsInstance(info, dict, msg=f"{pid}: {info}")
            self.assertTrue(info["product_ok"])
            self.assertIn(info["status"], {"out_of_stock", "in_stock", "unknown"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
