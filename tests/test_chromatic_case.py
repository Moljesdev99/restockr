"""ModRetro Chromatic Case: Codex Edition (OpenAI Supply Co.), page saved 2026-11-18.

The case page was "Restock soon" (expected Nov 19, US$35). It uses the same
Supply Co. button + Shopify availableForSale markers as the Chromatic console.
"""
import gzip
import json
from pathlib import Path

import pytest

import restockr_core as core

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
URL = "https://openai.com/supply/product/modretro-chromatic-case-codex-edition/"


@pytest.fixture(scope="module")
def html() -> str:
    return gzip.decompress((FIX / "supply_chromatic_case_restock_soon_2026-11-18.html.gz").read_bytes()).decode("utf-8")


@pytest.fixture(scope="module")
def case() -> dict:
    raw = json.loads((ROOT / "config.json").read_text())
    parsed = core.parse_config({"topic": "t", **raw}, require_topic=False)
    return {p["id"]: p for p in parsed["products"]}["chromatic-case"]


def _info(html, product):
    return core.extract(core._title_from_html(html), html, core._visible_from_html(html), product)


def test_case_in_live_config(case):
    assert case["url"] == URL
    assert case["type"] == "restock"
    assert not case["is_test"] and not case["primary"]
    assert case["remind_at"] is None  # the Chromatic reminder covers the day


def test_case_page_is_restock_soon(html, case):
    assert core.supply_html_stock(html) == "out_of_stock"
    info = _info(html, case)
    assert info["product_ok"]
    assert info["status"] == "out_of_stock"
    assert info["expected"] == "November 19"
    assert info["price"] == "US$35.00"


@pytest.mark.parametrize("flip_button,flip_json", [(True, True), (True, False), (False, True)])
def test_case_restock_flips_to_in_stock(html, case, flip_button, flip_json):
    out = html
    if flip_button:
        out = out.replace(' data-supply-out-of-stock="true"', "").replace(">Restock soon<", ">Add to cart<")
    if flip_json:
        out = out.replace('availableForSale\\":false', 'availableForSale\\":true')
    out = out.replace("Expected restock: November 19", "")
    assert core.supply_html_stock(out) == "in_stock"
    assert _info(out, case)["status"] == "in_stock"


def test_case_alert_card_and_buttons(html, case):
    info = _info(html, case)
    text = core.build_caption(info, {}, product=case)
    assert text.splitlines()[0] == "🧳 <b>ModRetro Chromatic Case Codex Edition</b>"
    assert "📅 <b>Expected date:</b>" in text and "US$35.00" in text and URL in text
    kb = core.build_alert_keyboard(case)["inline_keyboard"]
    assert kb[0] == [{"text": "🛒 View product", "url": URL}]
    datas = [b["callback_data"] for row in kb[1:] for b in row]
    assert datas == ["rs|b|chromatic-case", "rs|s|chromatic-case", "rs|k|chromatic-case"]


def test_case_buttons_and_reset_are_per_product(case):
    raw = json.loads((ROOT / "config.json").read_text())
    products = core.parse_config({"topic": "t", **raw}, require_topic=False)["products"]
    assert core._find_product(products, "chromatic-case")["id"] == "chromatic-case"
    assert core._find_product(products, "chromatic")["id"] == "chromatic"
    state = {"products": {}}
    core.product_state(state, case)["alert_mode"] = core.ALERT_MODE_SKIP
    assert core.is_muted(state, case)
    chromatic = core._find_product(products, "chromatic")
    assert not core.is_muted(state, chromatic)
    status = core.build_status_text(state, products)
    assert "<code>chromatic-case</code>" in status and "Chromatic Case" in status


def test_heartbeat_lists_case_with_distinct_button():
    raw = json.loads((ROOT / "config.json").read_text())
    cfg = core.parse_config({"topic": "t", **raw}, require_topic=False)
    labels = [b["text"] for row in core.build_heartbeat_keyboard(cfg)["inline_keyboard"] for b in row]
    assert "🎮 Open Chromatic Codex" in labels
    assert "🧳 Open Chromatic Case" in labels
    assert len(set(labels)) == len(labels)
    text = core.build_heartbeat_text(cfg, {}, fallback_product=None)
    assert "watches active" in text and "Chromatic Case" in text
    assert "Chromatic Codex" in text
