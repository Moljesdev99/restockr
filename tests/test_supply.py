"""OpenAI Supply Co. stock detection, from real pages saved on 2026-11-17.

The Chromatic page was "Restock soon" (expected Nov 19). The Pixel Nalgene page
was in stock. In-stock Chromatic variants are built from the real page the way
the site's own button code renders them (see supply_html_stock).
"""
import gzip
import re
from pathlib import Path

import pytest

import restockr_core as core

FIX = Path(__file__).parent / "fixtures"
CHROMATIC = {
    "id": "chromatic",
    "name": "Game Boy Chromatic Codex",
    "url": "https://openai.com/supply/co-lab/chromatic/",
}
NALGENE = {"id": "nalgene", "name": "Pixel Nalgene", "url": "https://openai.com/supply/product/pixel-nalgene/"}


def _load(name: str) -> str:
    return gzip.decompress((FIX / name).read_bytes()).decode("utf-8")


@pytest.fixture(scope="module")
def oos_html() -> str:
    return _load("supply_chromatic_restock_soon_2026-11-17.html.gz")


def _button(html: str) -> str:
    return re.search(r"<button[^>]*supply-add-to-cart[^>]*>.*?</button>", html, re.S).group(0)


def _in_stock_variant(html: str, *, label="Add to cart", hydrated=False, json_true=True, keep_date=False) -> str:
    old = _button(html)
    new = old.replace(' data-supply-out-of-stock="true"', "").replace(">Restock soon<", f">{label}<")
    if hydrated:
        new = new.replace(' disabled=""', "").replace(' data-disabled="true"', "")
    out = html.replace(old, new)
    if json_true:
        out = out.replace('availableForSale\\":false', 'availableForSale\\":true')
    if not keep_date:
        out = out.replace("Expected restock: November 19", "")
    return out


def _status(html: str, product=CHROMATIC) -> str:
    info = core.extract(core._title_from_html(html), html, core._visible_from_html(html), product)
    assert info["product_ok"]
    return info["status"]


def test_live_chromatic_is_out_of_stock(oos_html):
    assert core.supply_html_stock(oos_html) == "out_of_stock"
    assert _status(oos_html) == "out_of_stock"
    info = core.extract(core._title_from_html(oos_html), oos_html, core._visible_from_html(oos_html), CHROMATIC)
    assert info["expected"] == "November 19"
    assert info["price"] == "US$200.00"
    assert not core.looks_like_queue(core._title_from_html(oos_html), oos_html, CHROMATIC["url"])
    assert not core.looks_like_challenge(
        core._title_from_html(oos_html), oos_html, core._visible_from_html(oos_html), CHROMATIC
    )


def test_real_in_stock_supply_page():
    html = _load("supply_nalgene_in_stock_2026-11-17.html.gz")
    # Server-rendered in-stock button is still disabled (cart not loaded yet)
    assert 'disabled=""' in _button(html)
    assert core.supply_html_stock(html) == "in_stock"
    assert _status(html, NALGENE) == "in_stock"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # server-rendered (what the cloud watcher downloads)
        {"hydrated": True},  # Chrome DOM (box watcher)
        {"keep_date": True},  # site leaves the "Expected restock" line
        {"label": "Pre-order"},
        {"json_true": False},  # page data lags but the button flipped
    ],
)
def test_chromatic_restock_flips_to_in_stock(oos_html, kwargs):
    assert _status(_in_stock_variant(oos_html, **kwargs)) == "in_stock"


def test_restock_soon_text_elsewhere_does_not_hide_restock(oos_html):
    html = _in_stock_variant(oos_html) + '<div class="related"><span>Restock soon</span></div>'
    assert _status(html) == "in_stock"


def test_json_available_wins_over_stale_button(oos_html):
    html = oos_html.replace('availableForSale\\":false', 'availableForSale\\":true')
    assert _status(html) == "in_stock"


def test_sold_out_again(oos_html):
    old = _button(oos_html)
    html = oos_html.replace(old, old.replace(">Restock soon<", ">Out of stock<"))
    assert _status(html) == "out_of_stock"
