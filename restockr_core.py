#!/usr/bin/env python3
"""Restockr core (copied from the box watcher). Cloud entry point: restockr.py"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import signal
import sys
import time
import traceback
import uuid
import html as htmlmod
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import subprocess

DIR = Path(__file__).resolve().parent
CONFIG_PATH = DIR / "config.json"
STATE_PATH = DIR / "state.json"
LOCK_PATH = DIR / "watch.lock"
PROFILE_DIR = DIR / "chrome-profile"
LOG_PATH = DIR / "watch.log"

TZ = ZoneInfo("America/Mexico_City")
PRODUCT_URL = "https://openai.com/supply/co-lab/chromatic/"
HEARTBEAT_HOUR, HEARTBEAT_MINUTE = 9, 0
CONFIG_RELOAD_S = 300
# Set True in test mode so regular alerts are not enqueued and live state is not used.
TEST_MODE = False
DELIVERY_BACKOFF_CAP_S = 300
DELIVERY_MAX_ATTEMPTS = 12

CHECK_MIN_S, CHECK_MAX_S = 60, 120
IN_STOCK_REPEAT_S = 120
IN_STOCK_ALERT_MAX = 10
NAV_TIMEOUT_MS = 45_000
RENDER_WAIT_MS = 4_000
MAX_CONSECUTIVE_FAILURES_BEFORE_WARN = 5
BROWSER_RESTART_EVERY = 40
BACKOFF_CAP_S = 600

CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
]

log = logging.getLogger("watch")
_stop = False


def now_mx() -> datetime:
    return datetime.now(TZ)


def ts() -> str:
    return now_mx().strftime("%Y-%m-%d %H:%M:%S %Z")


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    # Log timestamps in Mexico City time
    fmt.converter = lambda secs: datetime.fromtimestamp(secs, TZ).timetuple()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        root.handlers.clear()
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


DEFAULT_PRODUCTS = [
    {
        "id": "chromatic",
        "name": "Game Boy Chromatic Codex",
        "short_name": "the Chromatic Codex",
        "url": "https://openai.com/supply/co-lab/chromatic/",
        "emoji": "🎮",
        "is_test": False,
        "primary": True,
    },
    {
        "id": "disney-ezra",
        "name": "Ezra Legacy Lightsaber Set",
        "short_name": "the Ezra Legacy Lightsaber Set",
        "url": "https://www.disneystore.com/ezra-legacy-lightsaber-set-star-wars-rebels-limited-edition-418149061750.html",
        "emoji": "⚔️",
        "is_test": True,
        "primary": False,
    },
]

PRODUCT_STATE_KEYS = (
    "last_status",
    "last_expected_restock",
    "in_stock_alerts_sent",
    "last_in_stock_alert_ts",
    "blocked_warning_sent",
    "consecutive_failures",
    "successful_checks",
    "product_image_url",
    "product_description",
    "product_price",
    "intro_test_sent",
)


RESTOCK_TYPES = {"restock", "product", "stock", ""}
GENERIC_TYPES = {
    "text_appears": "text_appears",
    "text-appears": "text_appears",
    "appears": "text_appears",
    "text_disappears": "text_disappears",
    "text-disappears": "text_disappears",
    "disappears": "text_disappears",
    "price_below": "price_below",
    "price-below": "price_below",
    "price": "price_below",
    "changed": "changed",
    "change": "changed",
    "hash": "changed",
    "content": "changed",
}


def entry_type(entry: dict | None) -> str:
    raw = ((entry or {}).get("type") or "restock")
    t = str(raw).strip().lower()
    if t in RESTOCK_TYPES:
        return "restock"
    if t in GENERIC_TYPES:
        return GENERIC_TYPES[t]
    return t


def is_restock(entry: dict | None) -> bool:
    return entry_type(entry) == "restock"


def _param(entry: dict, *keys, default=None):
    """Read a watch parameter from params/parameters, then the entry itself."""
    params = (entry.get("params") or entry.get("parameters") or {}) if entry else {}
    reserved = {
        "id", "name", "short_name", "url", "emoji", "is_test", "primary",
        "type", "params", "parameters", "image", "image_url", "cooldown",
        "button_text", "remind_on", "expected_date", "reminder", "remind_at",
    }
    for k in keys:
        if isinstance(params, dict) and k in params and params[k] is not None:
            return params[k]
        if entry and k in entry and entry[k] is not None and k not in reserved:
            return entry[k]
    return default


def parse_remind_at(entry: dict) -> datetime | None:
    """Optional per-watch reminder. Supports remind_on, or expected_date + reminder=true.

    Date-only values fire at 07:00 America/Mexico_City. No reminder unless configured.
    """
    raw = entry.get("remind_on")
    if not raw and entry.get("reminder") and entry.get("expected_date"):
        raw = entry.get("expected_date")
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "")
    # drop a trailing timezone offset for simple parsing; we always use TZ
    text = re.sub(r"([ap]m)$", "", text, flags=re.I)
    text = re.sub(r"\s*[+-]\d{2}:?\d{2}$", "", text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=7, minute=0, second=0)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            continue
    raise ValueError(f"Invalid remind_on/expected_date: {raw!r}")


def watch_fingerprint(entry: dict) -> str:
    payload = {
        "url": entry.get("url"),
        "type": entry_type(entry),
        "selector": _param(entry, "selector", "css"),
        "text": _param(entry, "text", "texts"),
        "pattern": _param(entry, "pattern", "regex"),
        "threshold": _param(entry, "threshold", "below", "max_price"),
        "json_path": _param(entry, "json_path"),
        "params": entry.get("params") or {},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def parse_config(cfg: dict, *, require_topic: bool = False) -> dict:
    """Normalize and validate a config dict. Raises ValueError if invalid."""
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    cfg = dict(cfg)
    if require_topic and not cfg.get("topic"):
        raise ValueError("config.json missing topic")
    products = cfg.get("products")
    watches = cfg.get("watches") or []
    if not products:
        if watches:
            products = []
        else:
            products = json.loads(json.dumps(DEFAULT_PRODUCTS))
            if cfg.get("product_url"):
                products[0]["url"] = cfg["product_url"]
    products = list(products or []) + list(watches)
    out = []
    seen_ids: set[str] = set()
    for i, p in enumerate(products):
        if not isinstance(p, dict) or not p.get("url") or not p.get("id"):
            raise ValueError(f"config product {i} missing id/url")
        if p["id"] in seen_ids:
            raise ValueError(f"config has duplicate id {p['id']!r}")
        seen_ids.add(p["id"])
        p = dict(p)
        p.setdefault("name", p["id"])
        p.setdefault("short_name", p["name"])
        p.setdefault("emoji", "🎮")
        p.setdefault("is_test", False)
        p.setdefault("primary", i == 0)
        p["type"] = entry_type(p)
        if p["type"] not in {"restock", "text_appears", "text_disappears", "price_below", "changed"}:
            raise ValueError(f"config product {i} ({p['id']}): unknown type {p.get('type')!r}")
        params = p.get("params") if isinstance(p.get("params"), dict) else None
        if params is None and isinstance(p.get("parameters"), dict):
            params = p.get("parameters")
        p["params"] = params or {}
        if p.get("image") and not p.get("image_url"):
            p["image_url"] = p["image"]
        if p.get("cooldown") is not None and p.get("cooldown") != "":
            try:
                p["cooldown"] = int(p["cooldown"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"config product {i} ({p['id']}): cooldown must be seconds (integer)") from exc
        try:
            p["remind_at"] = parse_remind_at(p)
        except ValueError as exc:
            raise ValueError(f"config product {i} ({p['id']}): {exc}") from exc
        p["fingerprint"] = watch_fingerprint(p)
        out.append(p)
    if not out:
        raise ValueError("config has no products or watches")
    if not any(p.get("primary") for p in out):
        out[0]["primary"] = True
    cfg["products"] = out
    cfg["product_url"] = cfg.get("product_url") or next(p["url"] for p in out if p.get("primary"))
    return cfg


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    try:
        return parse_config(cfg, require_topic=True)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def apply_new_config(old_cfg: dict, new_cfg: dict, state: dict) -> dict:
    """Keep per-watch state for unchanged ids; reset baseline if a watch's rule changed."""
    old_by_id = {p["id"]: p for p in (old_cfg or {}).get("products") or []}
    for p in new_cfg["products"]:
        pstate = product_state(state, p)
        previous = old_by_id.get(p["id"])
        if previous and previous.get("fingerprint") == p.get("fingerprint"):
            continue
        if previous:
            log.info("Watch %s changed; resetting baseline", p["id"])
        pstate["watch_baseline_set"] = False
        pstate["last_content_hash"] = None
        pstate["last_watch_triggered"] = False
        pstate["pending_change"] = None
        pstate["config_fingerprint"] = p.get("fingerprint")
    return new_cfg


def default_product_state() -> dict:
    return {
        "last_status": "unknown",
        "last_expected_restock": None,
        "in_stock_alerts_sent": 0,
        "last_in_stock_alert_ts": None,
        "blocked_warning_sent": False,
        "consecutive_failures": 0,
        "successful_checks": 0,
        "product_image_url": None,
        "product_description": None,
        "product_price": None,
        "intro_test_sent": False,
    }


def default_state() -> dict:
    return {
        "test_sent": False,
        "heartbeat_dates": [],
        "nov19_heads_up_sent": False,
        "pending_events": [],
        "products": {},
        # Legacy top-level chromatic fields (kept in sync)
        **default_product_state(),
    }


def _migrate_state(state: dict, products: list[dict]) -> dict:
    """Keep existing chromatic fields; nest per-product state."""
    bucket = state.setdefault("products", {})
    if not isinstance(bucket, dict):
        bucket = {}
        state["products"] = bucket
    primary = next((p for p in products if p.get("primary")), products[0])
    pid = primary["id"]
    if pid not in bucket:
        migrated = default_product_state()
        for k in PRODUCT_STATE_KEYS:
            if k in state and k != "intro_test_sent":
                migrated[k] = state[k]
        bucket[pid] = migrated
    for p in products:
        bucket.setdefault(p["id"], default_product_state())
    return state


def product_state(state: dict, product: dict) -> dict:
    bucket = state.setdefault("products", {})
    return bucket.setdefault(product["id"], default_product_state())


def sync_primary_state(state: dict, product: dict, pstate: dict) -> None:
    if not product.get("primary"):
        return
    for k in PRODUCT_STATE_KEYS:
        if k in pstate:
            state[k] = pstate[k]


def load_state(products: list[dict] | None = None) -> dict:
    state = default_state()
    if STATE_PATH.exists():
        try:
            saved = json.loads(STATE_PATH.read_text())
            if isinstance(saved, dict):
                state.update(saved)
        except Exception:
            log.exception("Could not read state.json; starting from defaults")
    if products:
        _migrate_state(state, products)
    return state


def save_state(state: dict) -> None:
    if TEST_MODE:
        return
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(STATE_PATH)


def acquire_lock() -> int:
    import fcntl

    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit("Another watcher instance is already running")
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd



def _tg_creds() -> tuple[str, str] | None:
    try:
        token = (DIR / ".tg_token").read_text().strip()
        chat = (DIR / ".tg_chat").read_text().strip()
    except Exception:
        return None
    if not token or not chat:
        return None
    return token, chat


def _tg_post(method: str, fields: dict, file_field: tuple[str, str, bytes] | None = None) -> dict:
    creds = _tg_creds()
    if not creds:
        raise RuntimeError("Telegram credentials missing")
    token, chat = creds
    fields = {"chat_id": chat, **fields}
    url = f"https://api.telegram.org/bot{token}/{method}"
    if file_field is None:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    name, filename, content = file_field
    boundary = "----gbwatch" + str(int(time.time() * 1000))
    parts: list[bytes] = []
    for key, val in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{val}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8")
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def tg_send(text: str) -> bool:
    """Plain Telegram text message (heartbeat, blocked, recovered)."""
    try:
        data = _tg_post(
            "sendMessage",
            {
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        ok = bool(data.get("ok"))
        if ok:
            log.info("telegram sent (text)")
        else:
            log.error("telegram sendMessage not ok: %s", data)
        return ok
    except Exception as exc:
        log.error("telegram failed: %s", exc)
        return False


_MONTHS_ES = {
    "january": "enero",
    "february": "febrero",
    "march": "marzo",
    "april": "abril",
    "may": "mayo",
    "june": "junio",
    "july": "julio",
    "august": "agosto",
    "september": "septiembre",
    "october": "octubre",
    "november": "noviembre",
    "december": "diciembre",
    "jan": "enero",
    "feb": "febrero",
    "mar": "marzo",
    "apr": "abril",
    "jun": "junio",
    "jul": "julio",
    "aug": "agosto",
    "sep": "septiembre",
    "sept": "septiembre",
    "oct": "octubre",
    "nov": "noviembre",
    "dec": "diciembre",
}

def format_date(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip()
    # Ignore placeholder warehouse dates (Disney uses 01/01/2500, year 2100, etc.)
    if re.search(r"\b(2100|2500)\b", text):
        return None
    m = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b",
        text,
        re.I,
    )
    if not m:
        return text
    full = {
        "jan": "January", "january": "January",
        "feb": "February", "february": "February",
        "mar": "March", "march": "March",
        "apr": "April", "april": "April",
        "may": "May",
        "jun": "June", "june": "June",
        "jul": "July", "july": "July",
        "aug": "August", "august": "August",
        "sep": "September", "sept": "September", "september": "September",
        "oct": "October", "october": "October",
        "nov": "November", "november": "November",
        "dec": "December", "december": "December",
    }
    name = full.get(m.group(1).lower().rstrip("."))
    if not name:
        return text
    return f"{name} {int(m.group(2))}"


# Back-compat alias (older call sites)
format_date_es = format_date


def status_label(status: str | None, expected: str | None) -> str:
    if status == "in_stock":
        return "In stock"
    if status == "out_of_stock" and expected:
        return "Coming soon"
    if status == "out_of_stock":
        return "Sold out"
    if status == "triggered":
        return "Condition met"
    if status == "idle":
        return "Waiting"
    if status == "changed":
        return "Content changed"
    if status == "ambiguous":
        return "Ambiguous (no alert)"
    return "To be confirmed"


def describe_es(english: str | None) -> str | None:
    """Shorten official page copy for the alert card. Keeps English."""
    if not english:
        return None
    one = re.sub(r"\s+", " ", english).strip()
    if len(one) > 140:
        one = one[:137].rsplit(" ", 1)[0] + "…"
    return one


def extract_image_url(html: str) -> str | None:
    pats = [
        r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
        r'property=["\']og:image:url["\'][^>]*content=["\']([^"\']+)["\']',
    ]
    for pat in pats:
        m = re.search(pat, html, re.I)
        if m:
            url = htmlmod.unescape(m.group(1).strip())
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http"):
                return url
    return None


def _fmt_usd(amt: str) -> str:
    try:
        return f"US${float(amt):.2f}"
    except ValueError:
        if "." not in amt:
            amt += ".00"
        return f"US${amt}"


def extract_price(html: str, visible: str) -> str | None:
    """Prefer the main product price; ignore recommended/related items later on the page."""
    m = re.search(
        r'role=["\']status["\'][^>]*>\s*\$(\d+(?:\.\d{2})?)\s*<',
        html,
        re.I,
    )
    if m:
        return _fmt_usd(m.group(1))
    h1 = re.search(
        r'<h1[^>]*class="[^"]*product-name[^"]*"[^>]*>.*?</h1>',
        html,
        re.I | re.S,
    )
    if h1:
        window = html[h1.start() : h1.end() + 4000]
        m = re.search(
            r'class="value"[^>]*content="(\d+(?:\.\d{2})?)"',
            window,
            re.I,
        )
        if not m:
            m = re.search(r"\$(\d+\.\d{2})\b", window)
        if m:
            return _fmt_usd(m.group(1))
    m = re.search(
        r'class="value"[^>]*content="(\d+(?:\.\d{2})?)"',
        html,
        re.I,
    )
    if m:
        return _fmt_usd(m.group(1))
    m = re.search(
        r'itemprop=["\']price["\'][^>]*content=["\'](\d+(?:\.\d{2})?)["\']',
        html,
        re.I,
    )
    if m:
        return _fmt_usd(m.group(1))
    amts = re.findall(
        r'"amount"\s*:\s*"(\d+(?:\.\d+)?)"\s*,\s*"currencyCode"\s*:\s*"USD"',
        html,
    )
    if amts:
        return _fmt_usd(amts[0])
    return None


def extract_description_en(html: str) -> str | None:
    # Prefer double-quoted content so inner apostrophes (''A Jedi...'') survive
    pats = [
        r'property=["\']og:description["\'][^>]*content="([^"]*)"',
        r"property=['\"]og:description['\"][^>]*content='([^']*)'",
        r'content="([^"]*)"[^>]*property=["\']og:description["\']',
        r'name=["\']description["\'][^>]*content="([^"]*)"',
    ]
    for pat in pats:
        m = re.search(pat, html, re.I)
        if m:
            return htmlmod.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    return None


def extract_expected_date(html: str, visible: str) -> str | None:
    source = visible or ""
    if "Expected restock:" not in source and "Expected restock:" in (html or ""):
        source = html
    m = re.search(
        r"Expected restock:\s*([A-Za-z]+\s+\d{1,2}(?:,?\s*\d{4})?)",
        source,
        re.I,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    # Disney-style: "Available Nov 17 at 8am PT"
    blob = f"{visible or ''}\n{html or ''}"
    m = re.search(
        r"Available\s+"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b"
        r"(?:\s+at\s+[^<\n]{0,40})?",
        blob,
        re.I,
    )
    if m:
        month = m.group(1)
        # Canonicalize abbreviation to full English month for storage
        full = {
            "jan": "January", "feb": "February", "mar": "March", "apr": "April",
            "may": "May", "jun": "June", "jul": "July", "aug": "August",
            "sep": "September", "sept": "September", "oct": "October",
            "nov": "November", "dec": "December",
        }
        key = month.lower().rstrip(".")
        name = full.get(key, month.capitalize() if key not in _MONTHS_ES else month)
        if key in full:
            name = full[key]
        else:
            name = month.capitalize()
        return f"{name} {int(m.group(2))}"
    return None


def cache_product_fields(pstate: dict, info: dict) -> None:
    if info.get("image_url"):
        pstate["product_image_url"] = info["image_url"]
    if info.get("description"):
        pstate["product_description"] = info["description"]
    if info.get("price"):
        pstate["product_price"] = info["price"]


def _prod(product: dict | None) -> dict:
    return product or {
        "id": "chromatic",
        "name": "Game Boy Chromatic Codex",
        "short_name": "la Chromatic Codex",
        "url": PRODUCT_URL,
        "emoji": "🎮",
        "is_test": False,
        "primary": True,
    }


def _bold(text: str, markup: str) -> str:
    if markup == "md":
        return f"**{text}**"
    return f"<b>{text}</b>"


def _esc(text: str, markup: str) -> str:
    if markup == "html":
        return htmlmod.escape(text)
    return text


def card_headline(
    product: dict | None,
    info: dict | None = None,
    pstate: dict | None = None,
    *,
    is_test: bool = False,
    event_line: str | None = None,
) -> str:
    """Plain-text ntfy title / first-line headline, no markup."""
    product = _prod(product)
    info = info or {}
    pstate = pstate or {}
    status = info.get("status") or pstate.get("last_status")
    test = is_test or bool(product.get("is_test"))
    name = product.get("name") or "Producto"
    emoji = product.get("emoji") or "🎮"
    short = product.get("short_name") or name
    if status == "in_stock":
        head = f"🟢 {short} is in stock!"
    elif event_line:
        head = re.sub(r"</?b>", "", event_line)
    else:
        head = f"{emoji} {name}"
    if test and not head.startswith("🧪"):
        return f"🧪 Test · {head}"
    return head


def build_caption(
    info: dict | None = None,
    state: dict | None = None,
    *,
    product: dict | None = None,
    is_test: bool = False,
    event_line: str | None = None,
    markup: str = "html",
) -> str:
    product = _prod(product)
    info = info or {}
    state = state or {}
    status = info.get("status") or state.get("last_status")
    expected = info.get("expected") or state.get("last_expected_restock")
    price = info.get("price") or state.get("product_price") or "To be confirmed"
    description = info.get("description") or state.get("product_description") or info.get("snippet")
    estado = status_label(status, expected)
    fecha = format_date(expected)
    name = product.get("name") or "Watch"
    emoji = product.get("emoji") or "🎮"
    short = product.get("short_name") or name
    url = product.get("url") or PRODUCT_URL
    test = is_test or bool(product.get("is_test"))
    restock = is_restock(product)

    lines: list[str] = []
    if test:
        lines.append(f"🧪 {_bold('Test', markup)}")
    if status == "in_stock":
        lines.append(f"🟢 {_bold(f'{short} is in stock!', markup)}")
    elif event_line:
        # event_line is stored as HTML; convert if needed
        ev = event_line
        if markup == "md":
            ev = ev.replace("<b>", "**").replace("</b>", "**")
        lines.append(ev)
    lines.append(f"{emoji} {_bold(name, markup)}")
    if description:
        lines.append(f"✨ {_esc(description, markup)}")
    lines.append("")
    lines.append(f"📦 {_bold('Status:', markup)} {_esc(estado, markup)}")
    if restock:
        if fecha:
            lines.append(f"📅 {_bold('Expected date:', markup)} {_esc(fecha, markup)}")
        elif status != "in_stock":
            lines.append(f"📅 {_bold('Expected date:', markup)} To be confirmed")
    if price and price != "To be confirmed":
        lines.append(f"💵 {_bold('Price:', markup)} {_esc(price, markup)}")
    elif restock:
        lines.append(f"💵 {_bold('Price:', markup)} {_esc(price, markup)}")
    lines.append("")
    lines.append(f"🔗 {url}")
    return "\n".join(lines)


def button_label(product: dict | None = None) -> str:
    product = product or {}
    custom = product.get("button_text")
    if custom:
        return str(custom)
    if is_restock(product):
        return "🛒 View product"
    return "🔗 Open page"


def entry_image(product: dict | None = None, info: dict | None = None, pstate: dict | None = None) -> str | None:
    info = info or {}
    pstate = pstate or {}
    product = product or {}
    return (
        info.get("image_url")
        or product.get("image")
        or product.get("image_url")
        or pstate.get("product_image_url")
    )


def _download_image(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": CHROME_UA, "Accept": "image/*,*/*"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if data and len(data) > 500:
            return data
    except Exception as exc:
        log.warning("Could not download product image: %s", exc)
    return None


def tg_send_card(
    info: dict | None = None,
    state: dict | None = None,
    *,
    product: dict | None = None,
    is_test: bool = False,
    event_line: str | None = None,
) -> bool:
    """Product card via sendPhoto; falls back to sendMessage if no image."""
    product = _prod(product)
    info = info or {}
    state = state or {}
    caption = build_caption(
        info, state, product=product, is_test=is_test, event_line=event_line, markup="html"
    )
    if len(caption) > 1024:
        caption = caption[:1020] + "…"
    image_url = entry_image(product, info, state)
    url = product.get("url") or PRODUCT_URL
    markup = json.dumps(
        {"inline_keyboard": [[{"text": button_label(product), "url": url}]]},
        ensure_ascii=False,
    )
    fields = {
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": markup,
    }
    try:
        if image_url:
            try:
                data = _tg_post("sendPhoto", {**fields, "photo": image_url})
                if data.get("ok"):
                    log.info("telegram sent (photo url) product=%s", product.get("id"))
                    return True
                log.warning("sendPhoto(url) rejected: %s", data)
            except Exception as exc:
                log.warning("sendPhoto(url) failed: %s", exc)
            blob = _download_image(image_url)
            if blob:
                try:
                    data = _tg_post(
                        "sendPhoto",
                        fields,
                        file_field=("photo", "product.png", blob),
                    )
                    if data.get("ok"):
                        log.info("telegram sent (photo upload) product=%s", product.get("id"))
                        return True
                    log.warning("sendPhoto(upload) rejected: %s", data)
                except Exception as exc:
                    log.warning("sendPhoto(upload) failed: %s", exc)
        data = _tg_post(
            "sendMessage",
            {
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
                "reply_markup": markup,
            },
        )
        ok = bool(data.get("ok"))
        if ok:
            log.info("telegram sent (text fallback) product=%s", product.get("id"))
        else:
            log.error("telegram fallback not ok: %s", data)
        return ok
    except Exception as exc:
        log.error("telegram card failed: %s", exc)
        return False


def ntfy(
    cfg: dict,
    *,
    title: str,
    message: str,
    priority: int = 3,
    tags: list[str] | None = None,
    click: str | None = None,
    attach: str | None = None,
    markdown: bool = False,
    action_label: str | None = None,
) -> tuple[bool, int]:
    """Send an ntfy message. Returns (ok, http_status)."""
    payload: dict = {
        "topic": cfg["topic"],
        "title": title,
        "message": message,
        "priority": priority,
    }
    if tags:
        payload["tags"] = tags
    if markdown:
        payload["markdown"] = True
    if click:
        payload["click"] = click
        payload["actions"] = [
            {
                "action": "view",
                "label": action_label or "🛒 View product",
                "url": click,
                "clear": True,
            }
        ]
    if attach:
        payload["attach"] = attach
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            status = getattr(resp, "status", 200) or 200
        log.info("ntfy sent: %s", title)
        return True, status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        log.error("ntfy failed (%s) HTTP %s: %s", title, exc.code, body)
        return False, int(exc.code)
    except Exception as exc:
        log.error("ntfy failed (%s): %s", title, exc)
        return False, 0


def ntfy_card(
    cfg: dict,
    product: dict | None = None,
    info: dict | None = None,
    pstate: dict | None = None,
    *,
    is_test: bool = False,
    event_line: str | None = None,
    priority: int = 3,
    tags: list[str] | None = None,
) -> tuple[bool, int]:
    product = _prod(product)
    info = info or {}
    pstate = pstate or {}
    title = card_headline(
        product, info, pstate, is_test=is_test, event_line=event_line
    )
    message = build_caption(
        info,
        pstate,
        product=product,
        is_test=is_test,
        event_line=event_line,
        markup="md",
    )
    image_url = entry_image(product, info, pstate)
    url = product.get("url") or PRODUCT_URL
    return ntfy(
        cfg,
        title=title,
        message=message,
        priority=priority,
        tags=tags,
        click=url,
        attach=image_url,
        markdown=True,
        action_label=button_label(product),
    )



def looks_like_challenge(title: str, html: str, visible: str, product: dict | None = None) -> bool:
    t = (title or "").lower()
    v = (visible or "").lower()
    html = html or ""
    if "just a moment" in t or "attention required" in t:
        return True
    if "modretro chromatic" in v or "restock soon" in v or "expected restock" in v:
        return False
    if "add-to-cart-release" in html or "product-oos-info" in html:
        return False
    if "currently out of stock" in v or "available nov" in v:
        return False
    if product:
        name = (product.get("name") or "").lower()
        if name and name.split("–")[0].strip() and name.split("–")[0].strip() in v:
            return False
        url = (product.get("url") or "").lower()
        if "disneystore.com" in url and "disneystore" in html.lower():
            return False
    if "challenge-platform" in html and "modretro" not in v and "disneystore" not in html.lower():
        return True
    if "cf-browser-verification" in html:
        return True
    return False


def extract(title: str, html: str, visible: str, product: dict | None = None) -> dict:
    vis = visible or ""
    html = html or ""
    low = vis.lower()
    html_low = html.lower()

    has_oos_attr = bool(
        re.search(r'data-supply-out-of-stock\s*=\s*["\']true["\']', html, re.I)
    )
    has_restock_soon = "restock soon" in low or "restock soon" in html_low
    has_coming_soon_btn = bool(
        re.search(
            r"<button[^>]*add-to-cart-release[^>]*disabled[^>]*>\s*Coming Soon",
            html,
            re.I | re.S,
        )
    )
    has_oos_text = bool(
        re.search(
            r"currently out of stock|product-oos-info-title\">\s*Out of Stock",
            html,
            re.I,
        )
    ) or ("out of stock" in low and "add-to-cart-release" in html_low)
    out_of_stock = has_oos_attr or has_restock_soon or has_coming_soon_btn or has_oos_text

    add_btn = bool(
        re.search(r'data-analytics\s*=\s*["\']supply-add-to-cart["\']', html, re.I)
    )
    enabled_add = False
    if add_btn and not has_oos_attr:
        enabled_add = True
    if re.search(
        r"<button[^>]*(add to cart|add to bag|buy now|checkout)[^>]*>",
        html,
        re.I,
    ):
        if not has_oos_attr and not has_coming_soon_btn:
            enabled_add = True
    # Disney (and similar): enabled add-to-cart-release / add-to-bag means in stock
    if re.search(
        r"<button[^>]*add-to-cart(?:-release)?[^>]*>",
        html,
        re.I,
    ) and not re.search(
        r"<button[^>]*add-to-cart(?:-release)?[^>]*disabled",
        html,
        re.I,
    ):
        if not has_oos_attr and not has_coming_soon_btn and not has_oos_text:
            enabled_add = True
    if re.search(
        r"<button[^>]*(?:add-to-bag|addToBag)[^>]*>",
        html,
        re.I,
    ) and not re.search(
        r"<button[^>]*(?:add-to-bag|addToBag)[^>]*disabled",
        html,
        re.I,
    ):
        if not out_of_stock:
            enabled_add = True

    expected = extract_expected_date(html, vis)

    name = ((product or {}).get("name") or "").lower()
    needles = [
        "modretro chromatic",
        "codex edition",
        "expected restock",
        "restock soon",
        "supply-add-to-cart",
        "add-to-cart-release",
        "product-oos-info",
        "currently out of stock",
    ]
    if name:
        # first distinctive words of the product name
        short = re.sub(r"[^a-z0-9 ]+", " ", name).strip()
        if short:
            needles.append(short)
    product_ok = any(s in low or s in html_low for s in needles if s)

    if out_of_stock:
        status = "out_of_stock"
    elif product_ok and (enabled_add or not has_restock_soon):
        # For non-Supply pages, require a positive in-stock signal
        if has_coming_soon_btn or has_oos_text:
            status = "out_of_stock"
        elif enabled_add or has_oos_attr is False and (
            add_btn or "supply-add-to-cart" in html_low
        ):
            status = "in_stock"
        elif enabled_add:
            status = "in_stock"
        elif product_ok and not out_of_stock and add_btn:
            status = "in_stock"
        else:
            # OpenAI in-stock pages have enabled add-to-cart; Disney in-stock
            # should have a non-disabled add button. If we only have product_ok
            # and no OOS markers, treat Supply-style pages as in stock.
            if "supply-add-to-cart" in html_low or "modretro" in html_low:
                status = "in_stock"
            elif enabled_add:
                status = "in_stock"
            else:
                status = "unknown"
    elif product_ok:
        status = "out_of_stock"
    else:
        status = "unknown"

    # Simplify final status
    if out_of_stock:
        status = "out_of_stock"
    elif enabled_add and product_ok:
        status = "in_stock"
    elif product_ok and add_btn and not has_oos_attr:
        status = "in_stock"
    elif product_ok and not out_of_stock and "supply-add-to-cart" in html_low:
        status = "in_stock"
    elif product_ok and not out_of_stock and not enabled_add and "disneystore" in html_low:
        status = "unknown"
    elif product_ok and not out_of_stock:
        status = "in_stock" if enabled_add or add_btn else status

    image_url = extract_image_url(html)
    price = extract_price(html, vis)
    description = describe_es(extract_description_en(html))

    return {
        "status": status,
        "out_of_stock": out_of_stock,
        "has_oos_attr": has_oos_attr,
        "has_restock_soon": has_restock_soon,
        "enabled_add": enabled_add,
        "expected": expected,
        "product_ok": product_ok,
        "title": title,
        "image_url": image_url,
        "price": price,
        "description": description,
    }


CHROME_BIN = "/usr/bin/google-chrome"
# UA that already passed Cloudflare in our one-off tests on this box
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _title_from_html(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def _visible_from_html(html: str) -> str:
    # Rough visible text for keyword checks; strip scripts/styles first
    cut = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    cut = re.sub(r"<style\b[^>]*>.*?</style>", " ", cut, flags=re.I | re.S)
    cut = re.sub(r"<[^>]+>", " ", cut)
    return re.sub(r"\s+", " ", cut)


def _select_with_bs4(html: str, selector: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        parts = [el.get_text(" ", strip=True) for el in soup.select(selector)]
        return "\n".join(p for p in parts if p)
    except Exception:
        return ""


def _simple_select(html: str, selector: str) -> str:
    """Very small CSS subset used when BeautifulSoup is unavailable: #id, .class, tag, [attr=value]."""
    sel = (selector or "").strip()
    if not sel:
        return ""
    html = html or ""
    pieces: list[str] = []
    for part in sel.split(","):
        part = part.strip()
        if not part:
            continue
        attr_m = re.fullmatch(
            r'(?:([A-Za-z][\w-]*)|#([A-Za-z_][\w-]*)|\.([A-Za-z_][\w-]*))?(?:\[([A-Za-z_][\w-]*)(?:=["\']?([^"\'\]]+)["\']?)?\])?',
            part,
        )
        if not attr_m:
            continue
        tag, id_, cls, attr, aval = attr_m.groups()
        pattern = r"<([A-Za-z][A-Za-z0-9]*)([^>]*)>(.*?)</\1>"
        for m in re.finditer(pattern, html, re.I | re.S):
            attrs = m.group(2)
            inner = m.group(3)
            if tag and m.group(1).lower() != tag.lower():
                continue
            if id_ and not re.search(
                rf'\bid\s*=\s*["\']{re.escape(id_)}["\']', attrs, re.I
            ):
                continue
            if cls and not re.search(
                rf'\bclass\s*=\s*["\'][^"\']*\b{re.escape(cls)}\b[^"\']*["\']',
                attrs,
                re.I,
            ):
                continue
            if attr:
                if aval is None:
                    if not re.search(rf"\b{re.escape(attr)}\b", attrs, re.I):
                        continue
                elif not re.search(
                    rf'''\b{re.escape(attr)}\s*=\s*["']?{re.escape(aval)}["']?''',
                    attrs,
                    re.I,
                ):
                    continue
            cut = re.sub(r"<[^>]+>", " ", inner)
            pieces.append(re.sub(r"\s+", " ", cut).strip())
    return "\n".join(p for p in pieces if p)


def select_matches(html: str, selector: str) -> list[tuple[str, str]]:
    """Return list of (text, inner_html) for each CSS match. Empty if none."""
    if not selector or not html:
        return []
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        found = []
        for el in soup.select(selector):
            text = el.get_text(" ", strip=True)
            inner = str(el)
            found.append((text, inner))
        if found:
            return found
    except Exception:
        pass
    text = _simple_select(html, selector)
    if text:
        return [(text, text)]
    return []


def select_text(html: str, selector: str) -> str:
    if not selector:
        return ""
    matches = select_matches(html, selector)
    return "\n".join(t for t, _ in matches if t)


def interpret_number(token: str) -> tuple[float | None, str | None]:
    """Parse a single numeric token. error is None, 'ambiguous', or 'unparseable'."""
    s = htmlmod.unescape(str(token)).replace("\xa0", " ").strip()
    s = re.sub(r"\s+", "", s)
    if not s or not re.search(r"\d", s):
        return None, "unparseable"
    s = s.replace("'", "")
    if not re.search(r"[.,]", s):
        try:
            return float(s), None
        except ValueError:
            return None, "unparseable"
    if "," in s and "." in s:
        if s.rfind(".") > s.rfind(","):
            try:
                return float(s.replace(",", "")), None
            except ValueError:
                return None, "unparseable"
        try:
            return float(s.replace(".", "").replace(",", ".")), None
        except ValueError:
            return None, "unparseable"
    sep = "," if "," in s else "."
    parts = s.split(sep)
    if len(parts) > 2:
        if all(len(p) == 3 for p in parts[1:]) and parts[0].isdigit():
            try:
                return float("".join(parts)), None
            except ValueError:
                return None, "unparseable"
        return None, "unparseable"
    left, right = parts
    if not left.isdigit() or not right.isdigit():
        return None, "unparseable"
    if len(right) == 3:
        return None, "ambiguous"
    if len(right) <= 2:
        try:
            return float(f"{left}.{right}"), None
        except ValueError:
            return None, "unparseable"
    return None, "unparseable"


_PRICE_TOKEN = re.compile(
    r"(?:USD|EUR|GBP|CAD|MXN|US\$|MX\$|CA\$|US|\$|€|£)\s*"
    r"(\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d+[.,]\d{1,2}|\d+)"
    r"|"
    r"(\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d+[.,]\d{1,2}|\d+)"
    r"(?:\s*(?:USD|EUR|GBP|CAD|MXN))?",
    re.I,
)


def extract_price_tokens(text: str) -> list[str]:
    if not text:
        return []
    s = htmlmod.unescape(str(text)).replace("\xa0", " ")
    out: list[str] = []
    for m in _PRICE_TOKEN.finditer(s):
        token = next(g for g in m.groups() if g)
        out.append(token)
    return out


def parse_price_detailed(raw) -> tuple[float | None, str | None]:
    """Return (amount, error) with error in {None, 'ambiguous', 'unparseable'}."""
    if raw is None or raw is False:
        return None, "unparseable"
    if isinstance(raw, bool):
        return None, "unparseable"
    if isinstance(raw, (int, float)):
        return float(raw), None
    text = htmlmod.unescape(str(raw)).replace("\xa0", " ").strip()
    if not text:
        return None, "unparseable"
    # Prefer the whole string if it is clearly one number
    compact = re.sub(r"^(?:USD|EUR|GBP|CAD|MXN|US\$|MX\$|CA\$|US|\$|€|£)\s*|\s*(?:USD|EUR|GBP|CAD|MXN)$", "", text, flags=re.I).strip()
    if re.fullmatch(r"\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d+[.,]\d{1,2}|\d+", compact):
        return interpret_number(compact)
    tokens = extract_price_tokens(text)
    if not tokens:
        return interpret_number(compact)
    parsed: list[float] = []
    saw_ambiguous = False
    for tok in tokens:
        amount, err = interpret_number(tok)
        if err == "ambiguous":
            saw_ambiguous = True
        elif amount is not None:
            parsed.append(amount)
    uniq = list(dict.fromkeys(parsed))
    if len(uniq) == 1:
        return uniq[0], None
    if len(uniq) > 1:
        return None, "ambiguous"
    if saw_ambiguous:
        return None, "ambiguous"
    return None, "unparseable"


def parse_price(raw) -> float | None:
    amount, _err = parse_price_detailed(raw)
    return amount


def _walk_json_path(node, path: str):
    cur = node
    parts = re.findall(r"[^.\[\]]+|\d+", path.replace("][", ".").replace("[", ".").replace("]", ""))
    for part in parts:
        if cur is None:
            return None
        if isinstance(cur, list):
            if part.isdigit():
                idx = int(part)
                cur = cur[idx] if 0 <= idx < len(cur) else None
            else:
                cur = cur[0] if cur and isinstance(cur[0], dict) else None
                if isinstance(cur, dict):
                    cur = cur.get(part)
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
            continue
        return None
    return cur


def _iter_json_blobs(html: str) -> list[object]:
    blobs: list[object] = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/(?:ld\+json|json)["\'][^>]*>(.*?)</script>',
        html or "",
        re.I | re.S,
    ):
        raw = htmlmod.unescape(m.group(1)).strip()
        if not raw:
            continue
        try:
            blobs.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blobs


def price_from_json_path(html: str, path: str) -> tuple[float | None, str | None]:
    values: list[float] = []
    ambiguous = False
    for blob in _iter_json_blobs(html):
        found = _walk_json_path(blob, path)
        if found is None:
            continue
        amount, err = parse_price_detailed(found)
        if err == "ambiguous":
            ambiguous = True
        elif amount is not None:
            values.append(amount)
    uniq = list(dict.fromkeys(values))
    if len(uniq) == 1:
        return uniq[0], None
    if len(uniq) > 1:
        return None, "ambiguous"
    if ambiguous:
        return None, "ambiguous"
    return None, "unparseable"


def _walk_jsonld_prices(node, out: list[float]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld_prices(item, out)
        return
    if not isinstance(node, dict):
        return
    for key in ("price", "lowPrice", "highPrice", "value"):
        if key in node:
            p, err = parse_price_detailed(node.get(key))
            if p is not None:
                out.append(p)
    offer = node.get("offers") or node.get("Offers")
    if offer is not None:
        _walk_jsonld_prices(offer, out)


def prices_from_jsonld(html: str) -> list[float]:
    prices: list[float] = []
    for blob in _iter_json_blobs(html):
        _walk_jsonld_prices(blob, prices)
    return list(dict.fromkeys(prices))


def format_money(amount: float | None, currency: str | None = None) -> str:
    if amount is None:
        return "To be confirmed"
    cur = (currency or "USD").upper()
    prefix = {"USD": "US$", "EUR": "€", "GBP": "£", "MXN": "MX$", "CAD": "CA$"}.get(
        cur, cur + " "
    )
    return f"{prefix}{amount:.2f}"


def _scope_or_raise(html: str, visible: str, entry: dict) -> tuple[str, str, str | None]:
    """Return (scoped_html, scoped_text, selector_or_None).

    If a selector is set but matches nothing, raise RuntimeError so the check
    counts as a failure — never as a valid change or baseline.
    """
    sel = _param(entry, "selector", "css")
    if sel is None or (isinstance(sel, str) and not str(sel).strip()):
        return html or "", visible or "", None
    sel = str(sel).strip()
    matches = select_matches(html, sel)
    if not matches:
        raise RuntimeError(
            f"Selector {sel!r} matched no content ({entry.get('id')})"
        )
    texts = [t for t, _h in matches]
    inners = [h for _t, h in matches]
    return "\n".join(inners), "\n".join(texts), sel


def _needle_present(html: str, visible: str, entry: dict) -> tuple[bool, str]:
    scoped_html, scoped_vis, sel = _scope_or_raise(html, visible, entry)
    case = bool(_param(entry, "case_sensitive", default=False))
    flags = 0 if case else re.I
    pattern = _param(entry, "pattern", "regex")
    if pattern:
        try:
            m = re.search(pattern, scoped_vis, flags) or re.search(
                pattern, scoped_html, flags
            )
        except re.error as exc:
            raise RuntimeError(
                f"Invalid regex in watch {entry.get('id')}: {exc}"
            ) from exc
        return (True, m.group(0)[:200]) if m else (False, "")
    raw = _param(entry, "text", "texts")
    if raw is None:
        raise RuntimeError(
            f"Watch {entry.get('id')} ({entry_type(entry)}) needs 'text' or 'pattern'"
        )
    texts = [raw] if isinstance(raw, str) else [str(x) for x in raw]
    hay_vis = scoped_vis if case else scoped_vis.lower()
    hay_html = scoped_html if case else scoped_html.lower()
    for t in texts:
        if not t:
            continue
        needle = t if case else t.lower()
        if needle in hay_vis or needle in hay_html:
            return True, t
    return False, ""


def extract_watch_price(html: str, visible: str, entry: dict) -> dict:
    """Locate a price. User locator (selector / regex / json_path) wins.

    Returns {amount, error, candidates, source}.
    error is None, 'ambiguous', or 'unparseable'.
    """
    result = {"amount": None, "error": None, "candidates": [], "source": None}
    scoped_html, scoped_vis, sel = _scope_or_raise(html, visible, entry)
    pattern = _param(entry, "pattern", "regex")
    json_path = _param(entry, "json_path")

    def _finish(amounts: list[float], source: str, ambiguous: bool = False):
        uniq = list(dict.fromkeys(amounts))
        result["candidates"] = uniq
        result["source"] = source
        if len(uniq) == 1:
            result["amount"] = uniq[0]
            result["error"] = None
        elif len(uniq) > 1 or ambiguous:
            result["error"] = "ambiguous"
        else:
            result["error"] = "unparseable"
        return result

    # 1) User regex (optionally scoped)
    if pattern:
        flags = 0 if _param(entry, "case_sensitive", default=False) else re.I
        try:
            matches = list(re.finditer(pattern, scoped_vis, flags)) or list(
                re.finditer(pattern, scoped_html, flags)
            )
        except re.error as exc:
            raise RuntimeError(
                f"Invalid regex in watch {entry.get('id')}: {exc}"
            ) from exc
        amounts: list[float] = []
        amb = False
        for m in matches:
            grp = m.group(1) if m.lastindex else m.group(0)
            amount, err = parse_price_detailed(grp)
            if err == "ambiguous":
                amb = True
            elif amount is not None:
                amounts.append(amount)
        return _finish(amounts, "regex", amb)

    # 2) User JSON path
    if json_path:
        amount, err = price_from_json_path(html, str(json_path))
        if amount is not None:
            return _finish([amount], "json_path")
        result["error"] = err or "unparseable"
        result["source"] = "json_path"
        return result

    # 3) User CSS selector: only prices inside it
    if sel:
        amounts = []
        amb = False
        for tok in extract_price_tokens(scoped_vis) or [scoped_vis]:
            amount, err = parse_price_detailed(tok)
            if err == "ambiguous":
                amb = True
            elif amount is not None:
                amounts.append(amount)
        return _finish(amounts, "selector", amb)

    # 4) No locator: collect page candidates; many different values → ambiguous
    amounts = []
    amb = False
    for p in prices_from_jsonld(html):
        amounts.append(p)
    labeled = extract_price(html, visible)
    if labeled:
        amount, err = parse_price_detailed(labeled)
        if err == "ambiguous":
            amb = True
        elif amount is not None:
            amounts.append(amount)
    for tok in extract_price_tokens(visible or ""):
        amount, err = parse_price_detailed(tok)
        if err == "ambiguous":
            amb = True
        elif amount is not None:
            amounts.append(amount)
    return _finish(amounts, "page", amb)


def _hash_text(text: str, ignore) -> str:
    value = text or ""
    if ignore:
        patterns = [ignore] if isinstance(ignore, str) else list(ignore)
        for pat in patterns:
            try:
                value = re.sub(pat, " ", value, flags=re.I)
            except re.error:
                value = value.replace(str(pat), " ")
    value = re.sub(r"\s+", " ", value).strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evaluate_generic(title: str, html: str, visible: str, entry: dict) -> dict:
    """Evaluate a non-restock watch against already-fetched HTML."""
    kind = entry_type(entry)
    image_url = extract_image_url(html) or entry.get("image") or entry.get("image_url")
    info: dict = {
        "status": "idle",
        "watch_type": kind,
        "triggered": False,
        "product_ok": True,
        "title": title,
        "image_url": image_url,
        "description": None,
        "price": None,
        "expected": None,
        "snippet": None,
        "content_hash": None,
        "matched_text": None,
        "threshold": None,
        "price_value": None,
    }
    if kind in ("text_appears", "text_disappears"):
        present, matched = _needle_present(html, visible, entry)
        triggered = present if kind == "text_appears" else (not present)
        info["triggered"] = triggered
        info["status"] = "triggered" if triggered else "idle"
        info["matched_text"] = matched or _param(entry, "text", "pattern", "regex")
        info["snippet"] = (matched or "")[:200] or None
        return info
    if kind == "price_below":
        threshold = _param(entry, "threshold", "below", "max_price")
        if threshold is None:
            raise RuntimeError(f"Watch {entry.get('id')} (price_below) needs a 'threshold'")
        threshold_val, thr_err = parse_price_detailed(threshold)
        if threshold_val is None:
            raise RuntimeError(f"Watch {entry.get('id')}: invalid price threshold ({thr_err})")
        threshold = threshold_val
        inclusive = bool(_param(entry, "inclusive", default=True))
        found = extract_watch_price(html, visible, entry)
        info["price_source"] = found.get("source")
        info["threshold"] = threshold
        if found.get("error") == "ambiguous" or (
            found.get("amount") is None and found.get("error") == "ambiguous"
        ):
            info["ambiguous"] = True
            info["triggered"] = False
            info["status"] = "ambiguous"
            info["snippet"] = (
                "Ambiguous price; not alerting. "
                f"Candidates: {found.get('candidates') or 'none'} "
                f"(source={found.get('source')})"
            )
            return info
        amount = found.get("amount")
        if amount is None:
            raise RuntimeError(
                f"Could not find a price on the page ({entry.get('id')}, "
                f"source={found.get('source')}, error={found.get('error')})"
            )
        triggered = amount <= threshold if inclusive else amount < threshold
        currency = _param(entry, "currency")
        info["triggered"] = triggered
        info["status"] = "triggered" if triggered else "idle"
        info["price_value"] = amount
        info["price"] = format_money(amount, currency)
        info["snippet"] = (
            f"{format_money(amount, currency)} "
            f"(threshold {format_money(threshold, currency)})"
        )
        return info
    if kind == "changed":
        _scoped_html, scoped_vis, sel = _scope_or_raise(html, visible, entry)
        basis = scoped_vis if sel else (visible or "")
        digest = _hash_text(basis, _param(entry, "ignore", "strip"))
        info["content_hash"] = digest
        info["snippet"] = (basis or "")[:200] or None
        info["status"] = "idle"
        info["triggered"] = False
        return info
    raise RuntimeError(f"Unknown watch type {kind!r} ({entry.get('id')})")


def generic_event_line(entry: dict, info: dict) -> str:
    kind = entry_type(entry)
    if kind == "text_appears":
        needle = info.get("matched_text") or _param(entry, "text") or "the expected text"
        return f"🟢 <b>“{_esc(str(needle), 'html')}” is now on the page</b>"
    if kind == "text_disappears":
        needle = info.get("matched_text") or _param(entry, "text") or "the expected text"
        return f"🟢 <b>“{_esc(str(needle), 'html')}” is no longer on the page</b>"
    if kind == "price_below":
        price = info.get("price") or "the current price"
        thr = format_money(info.get("threshold"), _param(entry, "currency"))
        return f"🟢 <b>Price is {htmlmod.escape(str(price))} (threshold {htmlmod.escape(thr)})</b>"
    if kind == "changed":
        return "🔔 <b>The page content changed</b>"
    return f"🔔 <b>{htmlmod.escape(entry.get('name') or 'Watch')} needs your attention</b>"


def evaluate_page(title: str, html: str, visible: str, product: dict) -> dict:
    """Shared evaluation after a page has been fetched."""
    product = _prod(product)
    if looks_like_challenge(title, html, visible, product):
        raise RuntimeError(
            f"Cloudflare/challenge page (product={product.get('id')}, title={title!r})"
        )
    if is_restock(product):
        info = extract(title, html, visible, product)
        if not info["product_ok"]:
            raise RuntimeError(
                f"Product content not found (product={product.get('id')}, "
                f"title={title!r}, len_html={len(html)})"
            )
        return info
    return evaluate_generic(title, html, visible, product)



def check_page(product: dict | None = None, _context=None) -> dict:
    """Load the product page with headless Chrome --dump-dom (the method that
    already succeeded on this box past Cloudflare). Each check starts and
    exits Chrome so memory does not accumulate."""
    product = _prod(product)
    url = product.get("url") or PRODUCT_URL
    cmd = [
        "timeout",
        "25s",
        CHROME_BIN,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1280,2000",
        "--virtual-time-budget=20000",
        "--timeout=20000",
        f"--user-agent={CHROME_UA}",
        "--no-first-run",
        "--no-default-browser-check",
        "--dump-dom",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Chrome timed out after 30s ({product.get('id')})") from exc
    html = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 and len(html) < 1000:
        err = proc.stderr.decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(f"Chrome exit {proc.returncode} ({product.get('id')}): {err}")
    title = _title_from_html(html)
    visible = _visible_from_html(html)
    return evaluate_page(title, html, visible, product)


def _channel_ready(ch: dict, now: datetime) -> bool:
    if ch.get("status") in ("sent", "skipped"):
        return False
    last = ch.get("last_attempt_ts")
    attempts = int(ch.get("attempts") or 0)
    if not last or attempts <= 0:
        return True
    try:
        previous = datetime.fromisoformat(str(last))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=TZ)
        delay = min(DELIVERY_BACKOFF_CAP_S, 2 ** min(attempts, 8))
        return (now - previous).total_seconds() >= delay
    except Exception:
        return True


def _event_product(event: dict, products_by_id: dict | None = None) -> dict:
    if products_by_id and event.get("watch_id") in products_by_id:
        return products_by_id[event["watch_id"]]
    return event.get("product") or {"id": event.get("watch_id"), "url": PRODUCT_URL, "name": event.get("watch_id")}


def _ntfy_on(cfg: dict) -> bool:
    if cfg.get("_ntfy_enabled") is False:
        return False
    topic = (cfg or {}).get("topic") or ""
    return bool(topic) and topic != "restockr-no-topic"


def enqueue_alert(
    cfg: dict,
    state: dict,
    product: dict,
    *,
    kind: str,
    info: dict | None = None,
    pstate: dict | None = None,
    event_line: str | None = None,
    priority: int = 4,
    tags: list[str] | None = None,
    text: str | None = None,
    title: str | None = None,
    use_card: bool = True,
    coalesce: bool = True,
) -> dict | None:
    """Queue an alert. Not marked delivered until each channel succeeds.

    If an event of the same watch+kind is already pending, its payload is
    updated to the latest values (coalesced) without resetting a channel
    that already succeeded.
    """
    if TEST_MODE:
        return None
    product = product or {}
    pstate = pstate or {}
    info = info or {}
    event = {
        "id": str(uuid.uuid4()),
        "watch_id": product.get("id"),
        "kind": kind,
        "event_line": event_line,
        "info": {
            k: info.get(k)
            for k in (
                "status", "expected", "price", "price_value", "threshold",
                "snippet", "image_url", "description", "title", "triggered",
                "content_hash", "matched_text", "watch_type",
            )
            if k in info
        },
        "pstate": {
            k: pstate.get(k)
            for k in ("product_image_url", "product_description", "product_price", "last_status", "last_expected_restock")
        },
        "product": {
            k: product.get(k)
            for k in (
                "id", "name", "short_name", "url", "emoji", "type",
                "button_text", "image", "image_url", "is_test", "primary",
            )
        },
        "is_test": bool(product.get("is_test")),
        "priority": priority,
        "tags": list(tags or []),
        "text": text,
        "title": title,
        "use_card": use_card,
        "created_ts": now_mx().isoformat(),
        "channels": {
            "telegram": {"status": "pending", "attempts": 0, "last_attempt_ts": None},
            "ntfy": {
                "status": "pending" if _ntfy_on(cfg) else "skipped",
                "attempts": 0,
                "last_attempt_ts": None,
            },
        },
    }
    pending = state.setdefault("pending_events", [])
    if coalesce:
        for existing in pending:
            if existing.get("watch_id") == event["watch_id"] and existing.get("kind") == kind:
                for key in ("event_line", "info", "pstate", "product", "text", "title", "priority", "tags", "use_card", "is_test"):
                    existing[key] = event[key]
                existing["updated_ts"] = event["created_ts"]
                log.info("Coalesced pending event watch=%s kind=%s", event["watch_id"], kind)
                save_state(state)
                return existing
    pending.append(event)
    save_state(state)
    return event


def _send_event_channel(cfg: dict, channel: str, event: dict, product: dict) -> bool:
    info = event.get("info") or {}
    pstate = event.get("pstate") or {}
    if channel == "telegram":
        if event.get("use_card"):
            return bool(
                tg_send_card(
                    info,
                    pstate,
                    product=product,
                    is_test=bool(event.get("is_test")),
                    event_line=event.get("event_line"),
                )
            )
        return bool(tg_send(event.get("text") or event.get("event_line") or ""))
    if channel == "ntfy":
        if not _ntfy_on(cfg):
            return True
        if event.get("use_card"):
            ok, _status = ntfy_card(
                cfg,
                product,
                info,
                pstate,
                is_test=bool(event.get("is_test")),
                event_line=event.get("event_line"),
                priority=int(event.get("priority") or 4),
                tags=event.get("tags"),
            )
            return bool(ok)
        title = event.get("title") or event.get("event_line") or "Restockr"
        title = re.sub(r"</?b>", "", title)
        ok, _status = ntfy(
            cfg,
            title=title,
            message=event.get("text") or title,
            priority=int(event.get("priority") or 3),
            tags=event.get("tags"),
        )
        return bool(ok)
    return False


def flush_pending(cfg: dict, state: dict, products: list[dict] | None = None) -> None:
    """Retry pending events with per-channel backoff. Persist after each attempt."""
    if TEST_MODE:
        return
    pending = list(state.get("pending_events") or [])
    if not pending:
        return
    now = now_mx()
    by_id = {p["id"]: p for p in (products or [])}
    still: list[dict] = []
    changed = False
    for event in pending:
        product = _event_product(event, by_id)
        channels = event.setdefault("channels", {})
        for name in ("telegram", "ntfy"):
            ch = channels.setdefault(name, {"status": "pending", "attempts": 0})
            if name == "ntfy" and not _ntfy_on(cfg) and ch.get("status") != "sent":
                if ch.get("status") != "skipped":
                    ch["status"] = "skipped"
                    changed = True
                continue
            if ch.get("status") in ("sent", "skipped"):
                continue
            if int(ch.get("attempts") or 0) >= DELIVERY_MAX_ATTEMPTS:
                continue
            if not _channel_ready(ch, now):
                continue
            ch["attempts"] = int(ch.get("attempts") or 0) + 1
            ch["last_attempt_ts"] = now.isoformat()
            changed = True
            try:
                ok = _send_event_channel(cfg, name, event, product)
            except Exception as exc:
                log.warning("Delivery %s failed for %s/%s: %s", name, event.get("watch_id"), event.get("kind"), exc)
                ok = False
            ch["status"] = "sent" if ok else "failed"
            log.info(
                "Delivery %s %s watch=%s kind=%s attempt=%s",
                name,
                "ok" if ok else "failed",
                event.get("watch_id"),
                event.get("kind"),
                ch["attempts"],
            )
        if all((channels.get(n) or {}).get("status") in ("sent", "skipped") for n in ("telegram", "ntfy")):
            continue
        still.append(event)
    if changed or len(still) != len(pending):
        state["pending_events"] = still
        save_state(state)


def emit_alert(cfg, state, product, **kwargs) -> dict | None:
    """Enqueue and immediately try to deliver."""
    event = enqueue_alert(cfg, state, product, **kwargs)
    flush_pending(cfg, state, [product])
    return event




def maybe_specials(cfg: dict, state: dict, product: dict, pstate: dict, status: str) -> None:
    n = now_mx()
    remind_at = product.get("remind_at")
    if isinstance(remind_at, str):
        try:
            remind_at = datetime.fromisoformat(remind_at)
            if remind_at.tzinfo is None:
                remind_at = remind_at.replace(tzinfo=TZ)
        except ValueError:
            remind_at = None
    if remind_at and n >= remind_at:
        day = remind_at.date().isoformat()
        if pstate.get("reminder_sent_on") != day:
            line = (
                "⏰ <b>Restock is today</b>"
                if is_restock(product)
                else "⏰ <b>Today is the expected date</b>"
            )
            emit_alert(
                cfg,
                state,
                product,
                kind="reminder",
                info={"status": status},
                pstate=pstate,
                event_line=line,
                priority=4,
                tags=["alarm_clock"],
            )
            pstate["reminder_sent_on"] = day
            save_state(state)

    today = n.date().isoformat()
    heartbeat_at = n.replace(
        hour=HEARTBEAT_HOUR, minute=HEARTBEAT_MINUTE, second=0, microsecond=0
    )
    if (
        product.get("primary")
        and n >= heartbeat_at
        and today not in state.get("heartbeat_dates", [])
        and status != "in_stock"
    ):
        emit_alert(
            cfg,
            state,
            product,
            kind="heartbeat",
            use_card=False,
            title="☁️ Cloud watcher is active",
            text="Cloud watcher is active, no stock yet.",
            event_line="☁️ Cloud watcher is active, no stock yet.",
            priority=3,
            tags=["green_heart"],
        )
        dates = list(state.get("heartbeat_dates", []))
        dates.append(today)
        state["heartbeat_dates"] = dates[-30:]
        save_state(state)


def _generic_due(product: dict, pstate: dict) -> bool:
    """True when a repeat alert is allowed because cooldown has elapsed."""
    cooldown = product.get("cooldown")
    if not cooldown:
        return False
    last = pstate.get("last_generic_alert_ts")
    if not last:
        return True
    try:
        previous = datetime.fromisoformat(str(last))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=TZ)
        return (now_mx() - previous).total_seconds() >= float(cooldown)
    except Exception:
        return True


def handle_generic(cfg: dict, state: dict, product: dict, info: dict) -> None:
    pstate = product_state(state, product)
    kind = entry_type(product)
    triggered = bool(info.get("triggered"))
    pstate["consecutive_failures"] = 0
    pstate["successful_checks"] = int(pstate.get("successful_checks") or 0) + 1
    cache_product_fields(pstate, info)

    if info.get("ambiguous"):
        log.warning(
            "CHECK AMBIGUOUS  product=%s  %s",
            product.get("id"),
            info.get("snippet") or "ambiguous value",
        )
        sync_primary_state(state, product, pstate)
        save_state(state)
        maybe_specials(cfg, state, product, pstate, "ambiguous")
        return

    if info.get("content_hash"):
        prev_hash = pstate.get("last_content_hash")
        if kind == "changed" and pstate.get("watch_baseline_set") and prev_hash and prev_hash != info["content_hash"]:
            triggered = True
            info["triggered"] = True
            info["status"] = "changed"
        # Always remember the latest hash so coalesced alerts use it
        pstate["last_content_hash"] = info["content_hash"]

    first = not pstate.get("watch_baseline_set")
    # Repeats only when a cooldown is configured and has elapsed.
    # A missing cooldown means: leading-edge only (or every change, for "changed").
    repeat_due = _generic_due(product, pstate)
    change_due = (not product.get("cooldown")) or repeat_due
    should_alert = False
    if first:
        should_alert = False
        pstate["pending_change"] = None
    elif kind == "changed":
        if triggered and change_due:
            should_alert = True
            pstate["pending_change"] = None
        elif triggered and not change_due:
            pstate["pending_change"] = {
                "info": dict(info),
                "event_line": generic_event_line(product, info),
                "updated_ts": now_mx().isoformat(),
            }
            log.info("Change during cooldown kept pending for %s", product.get("id"))
        elif (not triggered) and pstate.get("pending_change") and change_due:
            pending = pstate.pop("pending_change")
            info = pending.get("info") or info
            triggered = True
            should_alert = True
    else:
        was = bool(pstate.get("last_watch_triggered"))
        if triggered and not was:
            should_alert = True
            pstate["pending_change"] = None
        elif triggered and was and repeat_due:
            should_alert = True
            pstate["pending_change"] = None
        elif triggered and was and product.get("cooldown") and not repeat_due:
            pstate["pending_change"] = {
                "info": dict(info),
                "event_line": generic_event_line(product, info),
                "updated_ts": now_mx().isoformat(),
            }
        elif (not triggered) and pstate.get("pending_change") and repeat_due:
            pending = pstate.pop("pending_change")
            info = pending.get("info") or info
            should_alert = True

    due = change_due if kind == "changed" else repeat_due
    if not should_alert and pstate.get("pending_change") and due and not first:
        pending = pstate.pop("pending_change")
        info = pending.get("info") or info
        should_alert = True

    log.info(
        "CHECK OK  product=%s  type=%s  status=%s  triggered=%s  first=%s  alert=%s  price=%s",
        product.get("id"),
        kind,
        info.get("status"),
        triggered,
        first,
        should_alert,
        info.get("price") or "-",
    )

    if should_alert:
        line = generic_event_line(product, info)
        emit_alert(
            cfg,
            state,
            product,
            kind=f"generic:{kind}",
            info=info,
            pstate=pstate,
            event_line=line,
            priority=5 if triggered else 4,
            tags=["bell", "warning"],
        )
        pstate["last_generic_alert_ts"] = now_mx().isoformat()
        pstate["pending_change"] = None

    pstate["watch_baseline_set"] = True
    pstate["last_watch_triggered"] = triggered
    if info.get("price_value") is not None:
        pstate["last_watch_price"] = info["price_value"]
    if info.get("snippet"):
        pstate["last_watch_snippet"] = info["snippet"]
    if pstate.get("blocked_warning_sent"):
        emit_alert(
            cfg,
            state,
            product,
            kind="recovered",
            use_card=False,
            title=f"✅ Watcher recovered ({product.get('name')})",
            text=(
                f"I could read “{product.get('name')}” again. "
                f"Status: {status_label(info.get('status'), None)}."
            ),
            event_line=f"✅ Watcher recovered. I could read “{htmlmod.escape(product.get('name') or 'the page')}” again. "
            f"Status: {htmlmod.escape(status_label(info.get('status'), None))}.",
            priority=4,
            tags=["white_check_mark"],
        )
        pstate["blocked_warning_sent"] = False
    sync_primary_state(state, product, pstate)
    save_state(state)
    maybe_specials(cfg, state, product, pstate, info.get("status") or "idle")


def handle_result(cfg: dict, state: dict, product: dict, info: dict) -> None:
    if not is_restock(product):
        handle_generic(cfg, state, product, info)
        return
    pstate = product_state(state, product)
    status = info["status"]
    expected = info.get("expected")
    prev_status = pstate.get("last_status") or "unknown"
    prev_expected = pstate.get("last_expected_restock")

    cache_product_fields(pstate, info)

    if info.get("has_restock_soon"):
        restock_note = "Restock soon"
    else:
        restock_note = "no 'Restock soon'"

    log.info(
        "CHECK OK  product=%s  status=%s  restock_text=%s  expected=%s  price=%s  oos_attr=%s  enabled_add=%s",
        product.get("id"),
        status,
        restock_note,
        expected or "-",
        info.get("price") or "-",
        info.get("has_oos_attr"),
        info.get("enabled_add"),
    )

    always_test = bool(product.get("is_test"))

    # Date change (only after we already have a baseline)
    if (
        expected
        and prev_expected
        and expected != prev_expected
        and status != "unknown"
    ):
        emit_alert(
            cfg,
            state,
            product,
            kind="date_change",
            info=info,
            pstate=pstate,
            event_line="📅 <b>Restock date changed</b>",
            priority=4,
            tags=["calendar", "warning"],
        )

    if status == "in_stock":
        if prev_status != "in_stock":
            pstate["in_stock_alerts_sent"] = 0
        sent = int(pstate.get("in_stock_alerts_sent") or 0)
        last_ts = pstate.get("last_in_stock_alert_ts")
        due = True
        if sent > 0 and last_ts:
            try:
                last = datetime.fromisoformat(last_ts)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=TZ)
                due = (now_mx() - last).total_seconds() >= IN_STOCK_REPEAT_S
            except Exception:
                due = True
        if due and sent < IN_STOCK_ALERT_MAX:
            n = sent + 1
            emit_alert(
                cfg,
                state,
                product,
                kind="in_stock",
                info=info,
                pstate=pstate,
                priority=5,
                tags=["tada", "shopping", "warning"],
            )
            pstate["in_stock_alerts_sent"] = n
            pstate["last_in_stock_alert_ts"] = now_mx().isoformat()
    elif status == "out_of_stock" and prev_status == "in_stock":
        emit_alert(
            cfg,
            state,
            product,
            kind="sold_out",
            info=info,
            pstate=pstate,
            event_line="⚪ <b>Sold out again</b>",
            priority=4,
            tags=["white_circle", "shopping"],
        )
        pstate["in_stock_alerts_sent"] = 0
        pstate["last_in_stock_alert_ts"] = None

    if expected:
        pstate["last_expected_restock"] = expected
    if status in ("in_stock", "out_of_stock"):
        pstate["last_status"] = status
    pstate["successful_checks"] = int(pstate.get("successful_checks") or 0) + 1
    pstate["consecutive_failures"] = 0
    if pstate.get("blocked_warning_sent"):
        emit_alert(
            cfg,
            state,
            product,
            kind="recovered",
            use_card=False,
            title=f"✅ Watcher recovered ({product.get('name')})",
            text=(
                f"I could read “{product.get('name')}” again. "
                f"Status: {'in stock' if status == 'in_stock' else 'still out of stock'}."
            ),
            event_line=(
                f"✅ Watcher recovered. I could read “{htmlmod.escape(product.get('name') or 'the product')}” again. "
                f"Status: {'in stock' if status == 'in_stock' else 'still out of stock'}."
            ),
            priority=4,
            tags=["white_check_mark"],
        )
        pstate["blocked_warning_sent"] = False
    sync_primary_state(state, product, pstate)
    save_state(state)
    maybe_specials(cfg, state, product, pstate, status)


def handle_failure(cfg: dict, state: dict, product: dict, err: str) -> float:
    pstate = product_state(state, product)
    n = int(pstate.get("consecutive_failures") or 0) + 1
    pstate["consecutive_failures"] = n
    log.error(
        "CHECK FAIL  product=%s  consecutive=%s  error=%s",
        product.get("id"),
        n,
        err,
    )
    if n >= MAX_CONSECUTIVE_FAILURES_BEFORE_WARN and not pstate.get(
        "blocked_warning_sent"
    ):
        emit_alert(
            cfg,
            state,
            product,
            kind="blocked",
            use_card=False,
            title=f"⚠️ Watcher blocked ({product.get('name')})",
            text=(
                f"I could not read “{product.get('name')}” {n} times in a row "
                "(possible block). I will keep retrying."
            ),
            event_line=(
                f"⚠️ I could not read “{htmlmod.escape(product.get('name') or 'the page')}” "
                f"{n} times in a row (possible block). I will keep retrying."
            ),
            priority=4,
            tags=["warning"],
        )
        pstate["blocked_warning_sent"] = True
    sync_primary_state(state, product, pstate)
    save_state(state)
    if n >= MAX_CONSECUTIVE_FAILURES_BEFORE_WARN:
        extra = min(
            BACKOFF_CAP_S,
            60 * (2 ** min(n - MAX_CONSECUTIVE_FAILURES_BEFORE_WARN, 4)),
        )
        return float(extra)
    return 0.0


def sleep_jitter(extra: float = 0.0, in_stock_repeat: bool = False) -> None:
    if in_stock_repeat:
        base = IN_STOCK_REPEAT_S
    else:
        base = random.uniform(CHECK_MIN_S, CHECK_MAX_S)
    total = base + extra
    log.info("sleeping %.1fs", total)
    end = time.monotonic() + total
    while time.monotonic() < end and not _stop:
        time.sleep(min(1.0, end - time.monotonic()))


def on_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    log.info("Received signal %s, stopping", signum)


def maybe_rotate_log() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 20 * 1024 * 1024:
            bak = DIR / "watch.log.1"
            if bak.exists():
                bak.unlink()
            LOG_PATH.rename(bak)
    except Exception:
        pass


def any_in_stock_repeat(state: dict, products: list[dict], last_results: dict) -> bool:
    for p in products:
        info = last_results.get(p["id"]) or {}
        pstate = product_state(state, p)
        if info.get("status") == "in_stock" and int(
            pstate.get("in_stock_alerts_sent") or 0
        ) < IN_STOCK_ALERT_MAX:
            return True
    return False


def main() -> int:
    setup_logging()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    lock_fd = acquire_lock()
    cfg = load_config()
    products = cfg["products"]
    state = load_state(products)
    log.info(
        "Watcher starting pid=%s products=%s",
        os.getpid(),
        ",".join(p["id"] for p in products),
    )

    # If we start after today's 9:00 heartbeat, skip it — the test
    # notification already confirms the watcher is on.
    _n = now_mx()
    _hb = _n.replace(hour=HEARTBEAT_HOUR, minute=HEARTBEAT_MINUTE, second=0, microsecond=0)
    if (not state.get("test_sent") and _n >= _hb
            and _n.date().isoformat() not in state.get("heartbeat_dates", [])):
        dates = list(state.get("heartbeat_dates", []))
        dates.append(_n.date().isoformat())
        state["heartbeat_dates"] = dates
        save_state(state)

    # Do not auto-resend the original Chromatic startup test.
    # Per-product intro tests are sent manually or via intro_test_sent.

    try:
        while not _stop:
            maybe_rotate_log()
            flush_pending(cfg, state, products)
            extra = 0.0
            last_results: dict = {}
            for product in products:
                if _stop:
                    break
                try:
                    info = check_page(product)
                    handle_result(cfg, state, product, info)
                    last_results[product["id"]] = info
                except Exception as exc:
                    extra = max(extra, handle_failure(cfg, state, product, f"{exc}"))
                    last_results[product["id"]] = None
            if _stop:
                break
            sleep_jitter(
                extra=extra,
                in_stock_repeat=any_in_stock_repeat(state, products, last_results),
            )
        log.info("Stopped cleanly")
        return 0
    finally:
        try:
            flush_pending(cfg, state, products)
            save_state(state)
        except Exception:
            log.exception("Final flush/save failed")
        try:
            os.close(lock_fd)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        setup_logging()
        log.error("Fatal:\n%s", traceback.format_exc())
        sys.exit(1)
