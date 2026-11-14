#!/usr/bin/env python3
"""Restockr core (copied from the box watcher). Cloud entry point: restockr.py"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import signal
import sys
import time
import traceback
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
END_AT = datetime(2026, 11, 21, 23, 59, 59, tzinfo=TZ)
NOV19 = datetime(2026, 11, 19, 7, 0, 0, tzinfo=TZ)
HEARTBEAT_HOUR, HEARTBEAT_MINUTE = 9, 0

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
        "short_name": "la Chromatic Codex",
        "url": "https://openai.com/supply/co-lab/chromatic/",
        "emoji": "🎮",
        "is_test": False,
        "primary": True,
    },
    {
        "id": "disney-ezra",
        "name": "Ezra Legacy Lightsaber Set",
        "short_name": "el Ezra Legacy Lightsaber Set",
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


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    if not cfg.get("topic"):
        raise SystemExit("config.json missing topic")
    products = cfg.get("products")
    if not products:
        products = json.loads(json.dumps(DEFAULT_PRODUCTS))
        if cfg.get("product_url"):
            products[0]["url"] = cfg["product_url"]
    # Normalize
    out = []
    for i, p in enumerate(products):
        if not p.get("url") or not p.get("id"):
            raise SystemExit(f"config product {i} missing id/url")
        p = dict(p)
        p.setdefault("name", p["id"])
        p.setdefault("short_name", p["name"])
        p.setdefault("emoji", "🎮")
        p.setdefault("is_test", False)
        p.setdefault("primary", i == 0)
        out.append(p)
    if not any(p.get("primary") for p in out):
        out[0]["primary"] = True
    cfg["products"] = out
    cfg["product_url"] = cfg.get("product_url") or next(p["url"] for p in out if p.get("primary"))
    return cfg


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

# Official page copy → brief Spanish one-liner
_DESC_ES = {
    "create your own games with codex, load them onto a cartridge, and play on a handheld":
        "Crea tus propios juegos con Codex, cárgalos en un cartucho y júegalos en esta portátil.",
    "create your own games with codex":
        "Crea tus propios juegos con Codex, cárgalos en un cartucho y júegalos en esta portátil.",
    "limited edition ezra lightsaber hilt set":
        "Set limitado con dos empuñaduras Legacy de Ezra Bridger, con sonido e iluminación.",
    "ezra lightsaber hilt set featuring two":
        "Set limitado con dos empuñaduras Legacy de Ezra Bridger, con sonido e iluminación.",
}


def format_date_es(raw: str | None) -> str | None:
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
    month = _MONTHS_ES.get(m.group(1).lower().rstrip("."))
    if not month:
        return text
    return f"{int(m.group(2))} de {month}"


def status_label(status: str | None, expected: str | None) -> str:
    if status == "in_stock":
        return "Disponible"
    if status == "out_of_stock" and expected:
        return "Próximamente"
    if status == "out_of_stock":
        return "Agotado"
    return "Por confirmar"


def describe_es(english: str | None) -> str | None:
    if not english:
        return None
    low = english.lower()
    for needle, es in _DESC_ES.items():
        if needle in low:
            return es
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
        head = f"🟢 ¡Ya hay stock de {short}!"
    elif event_line:
        head = re.sub(r"</?b>", "", event_line)
    else:
        head = f"{emoji} {name}"
    if test and not head.startswith("🧪"):
        return f"🧪 Prueba · {head}"
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
    price = info.get("price") or state.get("product_price") or "Por confirmar"
    description = info.get("description") or state.get("product_description")
    estado = status_label(status, expected)
    fecha = format_date_es(expected)
    name = product.get("name") or "Producto"
    emoji = product.get("emoji") or "🎮"
    short = product.get("short_name") or name
    url = product.get("url") or PRODUCT_URL
    test = is_test or bool(product.get("is_test"))

    lines: list[str] = []
    if test:
        lines.append(f"🧪 {_bold('Prueba', markup)}")
    if status == "in_stock":
        lines.append(f"🟢 {_bold(f'¡Ya hay stock de {short}!', markup)}")
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
    lines.append(f"📦 {_bold('Estado:', markup)} {_esc(estado, markup)}")
    if fecha:
        lines.append(f"📅 {_bold('Fecha prevista:', markup)} {_esc(fecha, markup)}")
    elif status != "in_stock":
        lines.append(f"📅 {_bold('Fecha prevista:', markup)} Por confirmar")
    lines.append(f"💵 {_bold('Precio:', markup)} {_esc(price, markup)}")
    lines.append("")
    lines.append(f"🔗 {url}")
    return "\n".join(lines)


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
    image_url = info.get("image_url") or state.get("product_image_url")
    url = product.get("url") or PRODUCT_URL
    markup = json.dumps(
        {"inline_keyboard": [[{"text": "🛒 Ver producto", "url": url}]]},
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
                "label": action_label or "🛒 Ver producto",
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
    image_url = info.get("image_url") or pstate.get("product_image_url")
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
        action_label="🛒 Ver producto",
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
    if looks_like_challenge(title, html, visible, product):
        raise RuntimeError(
            f"Cloudflare/challenge page (product={product.get('id')}, title={title!r})"
        )
    info = extract(title, html, visible, product)
    if not info["product_ok"]:
        raise RuntimeError(
            f"Product content not found (product={product.get('id')}, "
            f"title={title!r}, len_html={len(html)})"
        )
    return info


def maybe_specials(cfg: dict, state: dict, product: dict, pstate: dict, status: str) -> None:
    n = now_mx()
    # Chromatic-only Nov 19 heads-up
    if product.get("primary") and (
        not state.get("nov19_heads_up_sent")
        and n.date() == NOV19.date()
        and n >= NOV19
    ):
        ntfy_card(
            cfg,
            product,
            pstate=pstate,
            event_line="⏰ <b>Hoy es el restock</b>",
            priority=4,
            tags=["alarm_clock", "gameboy"],
        )
        tg_send_card(
            state=pstate,
            product=product,
            event_line="⏰ <b>Hoy es el restock</b>",
        )
        state["nov19_heads_up_sent"] = True
        save_state(state)

    today = n.date().isoformat()
    heartbeat_at = n.replace(
        hour=HEARTBEAT_HOUR, minute=HEARTBEAT_MINUTE, second=0, microsecond=0
    )
    # Heartbeat once per day, only after the primary product check
    if (
        product.get("primary")
        and n >= heartbeat_at
        and today not in state.get("heartbeat_dates", [])
        and status != "in_stock"
    ):
        ntfy(
            cfg,
            title="☁️ Vigilante en la nube activo",
            message="Vigilante en la nube activo, sin stock aún.",
            priority=3,
            tags=["green_heart"],
        )
        tg_send("☁️ Vigilante en la nube activo, sin stock aún.")
        dates = list(state.get("heartbeat_dates", []))
        dates.append(today)
        state["heartbeat_dates"] = dates[-30:]
        save_state(state)


def handle_result(cfg: dict, state: dict, product: dict, info: dict) -> None:
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
        ntfy_card(
            cfg,
            product,
            info,
            pstate,
            is_test=always_test,
            event_line="📅 <b>Cambió la fecha de restock</b>",
            priority=4,
            tags=["calendar", "warning"],
        )
        tg_send_card(
            info,
            pstate,
            product=product,
            is_test=always_test,
            event_line="📅 <b>Cambió la fecha de restock</b>",
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
            ntfy_card(
                cfg,
                product,
                info,
                pstate,
                is_test=always_test,
                priority=5,
                tags=["tada", "shopping", "warning"],
            )
            tg_send_card(info, pstate, product=product, is_test=always_test)
            pstate["in_stock_alerts_sent"] = n
            pstate["last_in_stock_alert_ts"] = now_mx().isoformat()
    elif status == "out_of_stock" and prev_status == "in_stock":
        ntfy_card(
            cfg,
            product,
            info,
            pstate,
            is_test=always_test,
            event_line="⚪ <b>Se agotó de nuevo</b>",
            priority=4,
            tags=["white_circle", "shopping"],
        )
        tg_send_card(
            info,
            pstate,
            product=product,
            is_test=always_test,
            event_line="⚪ <b>Se agotó de nuevo</b>",
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
        ntfy(
            cfg,
            title=f"✅ Vigilante recuperado ({product.get('name')})",
            message=(
                f"Ya pude leer «{product.get('name')}» otra vez. "
                f"Estado: {'con stock' if status == 'in_stock' else 'sin stock aún'}."
            ),
            priority=4,
            tags=["white_check_mark"],
        )
        tg_send(
            f"✅ Vigilante recuperado. Ya pude leer «{htmlmod.escape(product.get('name') or 'el producto')}» otra vez. "
            f"Estado: {'con stock' if status == 'in_stock' else 'sin stock aún'}."
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
        ntfy(
            cfg,
            title=f"⚠️ Vigilante bloqueado ({product.get('name')})",
            message=(
                f"No pude leer «{product.get('name')}» {n} veces seguidas "
                "(posible Cloudflare u otro bloqueo). Sigo reintentando."
            ),
            priority=4,
            tags=["warning"],
        )
        tg_send(
            f"⚠️ No pude leer «{htmlmod.escape(product.get('name') or 'el producto')}» "
            f"{n} veces seguidas (posible bloqueo). Sigo reintentando."
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
        "Watcher starting pid=%s end_at=%s products=%s",
        os.getpid(),
        END_AT.isoformat(),
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
            if now_mx() >= END_AT:
                log.info("Reached end date %s, exiting.", END_AT.isoformat())
                return 0
            maybe_rotate_log()
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
