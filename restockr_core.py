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
DISPLAY_TZ = ZoneInfo("America/Asuncion")
DISPLAY_TZ_LABEL = "PYT"
PRODUCT_URL = "https://openai.com/supply/co-lab/chromatic/"
HEARTBEAT_HOUR, HEARTBEAT_MINUTE = 9, 0
TELEGRAM_TEXT_MAX = 4096
CONFIG_RELOAD_S = 300
# Set True in test mode so regular alerts are not enqueued and live state is not used.
TEST_MODE = False
DELIVERY_BACKOFF_CAP_S = 300
DELIVERY_MAX_ATTEMPTS = 12

CHECK_MIN_S, CHECK_MAX_S = 60, 120
TIGHT_CHECK_MIN_S, TIGHT_CHECK_MAX_S = 60, 75
IN_STOCK_REPEAT_S = 120
# Default number of in-stock repeats (every IN_STOCK_REPEAT_S, so about 1 hour).
# "🔔 Keep alerting" removes the cap; "✅ Bought it" / "🚫 Not buying" mute the watch.
IN_STOCK_ALERT_MAX = 30
# Telegram button taps / commands (getUpdates short polling, no webhook).
TG_POLL_S = 3.0
# Updates are confirmed (deleted from Telegram's queue) only after this many
# seconds, so a second watcher sharing the bot can still read every tap.
TG_UPDATE_RETAIN_S = 12 * 3600
# Only one watcher should answer text commands (/status, /reset); the cloud
# watcher sets this to True. Both watchers still apply the state change.
TG_REPLY_COMMANDS = False
TG_COMMAND_MAX_AGE_S = 600
# A muted watch ("Bought it" / "Not buying") is only re-read this often, so the
# heartbeat stays fresh without spending a check every minute.
MUTED_CHECK_S = 1800
ALERT_MODE_KEEP, ALERT_MODE_BOUGHT, ALERT_MODE_SKIP = "keep", "bought", "skip"
MUTED_MODES = (ALERT_MODE_BOUGHT, ALERT_MODE_SKIP)
# Alert kinds that get the Bought it / Not buying / Keep alerting buttons.
CONTROL_KINDS = ("in_stock", "queue", "queue_ended")
# Product id used by the buttons of a test card; taps on it change nothing.
TEST_CALLBACK_ID = "restockr-test"
# Virtual waiting room (Queue-it). The "open" alert repeats up to QUEUE_ALERT_MAX
# times, QUEUE_ALERT_GAP_S apart, while the page keeps redirecting to the queue.
# The "over" alert fires once, after QUEUE_END_CONFIRM reachable reads in a row
# (so a single flaky read does not end the waiting room).
QUEUE_ALERT_MAX = 3
QUEUE_ALERT_GAP_S = 180
QUEUE_END_CONFIRM = 2
QUEUE_OPEN_LINE = "🚦 <b>The virtual waiting room is open! Join now</b>"
QUEUE_ENDED_LINE = "✅ <b>The waiting room is over, the product page is open again</b>"
NAV_TIMEOUT_MS = 45_000
RENDER_WAIT_MS = 4_000
MAX_CONSECUTIVE_FAILURES_BEFORE_WARN = 5
BROWSER_RESTART_EVERY = 40
BACKOFF_CAP_S = 600
REMINDER_DAY_BACKOFF_CAP_S = 30

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
        "timezone", "tz",
    }
    for k in keys:
        if isinstance(params, dict) and k in params and params[k] is not None:
            return params[k]
        if entry and k in entry and entry[k] is not None and k not in reserved:
            return entry[k]
    return default


def _remind_timezone(entry: dict) -> ZoneInfo:
    raw = entry.get("timezone") or entry.get("tz")
    if raw is None or str(raw).strip() == "":
        return DISPLAY_TZ
    name = str(raw).strip()
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ValueError(f"Invalid timezone: {name!r}") from exc


def parse_remind_at(entry: dict) -> datetime | None:
    """Optional per-watch reminder. Supports remind_on, or expected_date + reminder=true.

    Naive wall-clock times are interpreted in America/Asuncion (PYT), or in
    the watch's optional `timezone` field. Date-only values fire at 07:00 in
    that zone. An explicit UTC offset or trailing Z is honored as-is.
    The daily heartbeat schedule is separate and still uses America/Mexico_City.
    """
    raw = entry.get("remind_on")
    if not raw and entry.get("reminder") and entry.get("expected_date"):
        raw = entry.get("expected_date")
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    tz = _remind_timezone(entry)
    normalized = text.replace("T", " ").strip()
    iso = normalized[:-1] + "+00:00" if normalized.endswith(("Z", "z")) else normalized
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
    if dt is not None:
        if dt.tzinfo is not None:
            return dt
        if len(normalized.split()) == 1 and ":" not in normalized:
            dt = dt.replace(hour=7, minute=0, second=0)
        return dt.replace(tzinfo=tz)
    parsed = re.sub(r"([ap]m)$", "", normalized, flags=re.I)
    parsed = re.sub(r"\s*[+-]\d{2}:?\d{2}$", "", parsed).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(parsed, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=7, minute=0, second=0)
            return dt.replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"Invalid remind_on/expected_date: {raw!r}")


def reminder_local_dates(products) -> set:
    """Calendar dates in America/Asuncion that have a configured reminder."""
    dates = set()
    for product in products or []:
        value = product.get("remind_at")
        if not isinstance(value, datetime):
            try:
                value = parse_remind_at(product)
            except ValueError:
                value = None
        if not value:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=DISPLAY_TZ)
        dates.add(value.astimezone(DISPLAY_TZ).date())
    return dates


def check_interval_range(
    now=None,
    products=None,
    in_stock_repeat: bool = False,
) -> tuple[float, float]:
    """Seconds between checks. Tightens to 60-75s on a reminder day (PYT).

    While an in-stock alert is repeating, checks never get slower than the
    normal (or reminder-day) interval: one watch staying in stock must not
    slow down the others on their restock day.
    """
    low, high = float(CHECK_MIN_S), float(CHECK_MAX_S)
    if is_reminder_day(now, products):
        low, high = float(TIGHT_CHECK_MIN_S), float(TIGHT_CHECK_MAX_S)
    if in_stock_repeat:
        repeat = float(IN_STOCK_REPEAT_S)
        return min(low, repeat), min(high, repeat)
    return low, high


def is_reminder_day(now=None, products=None) -> bool:
    """True when today (America/Asuncion) is a configured reminder date."""
    if not products:
        return False
    now = now or datetime.now(DISPLAY_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    return now.astimezone(DISPLAY_TZ).date() in reminder_local_dates(products)


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


def _tg_post(
    method: str,
    fields: dict,
    file_field: tuple[str, str, bytes] | None = None,
    *,
    with_chat: bool = True,
) -> dict:
    creds = _tg_creds()
    if not creds:
        raise RuntimeError("Telegram credentials missing")
    token, chat = creds
    fields = {"chat_id": chat, **fields} if with_chat else dict(fields)
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


def tg_send(text: str, reply_markup: dict | str | None = None) -> bool:
    """Plain Telegram text message (heartbeat, blocked, recovered)."""
    try:
        payload = {
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = (
                reply_markup
                if isinstance(reply_markup, str)
                else json.dumps(reply_markup, ensure_ascii=False)
            )
        data = _tg_post("sendMessage", payload)
        ok = bool(data.get("ok"))
        if ok:
            log.info("telegram sent (text)")
        else:
            log.error("telegram sendMessage not ok: %s", data)
        return ok
    except Exception as exc:
        log.error("telegram failed: %s", exc)
        return False


# ---------- alert controls (inline buttons + commands) ----------
_poll_ctx: dict = {}
_last_poll = 0.0


def alert_mode(state: dict | None, product: dict | None) -> str | None:
    if not state or not product or not product.get("id"):
        return None
    bucket = state.get("products") or {}
    return (bucket.get(product["id"]) or {}).get("alert_mode")


def is_muted(state: dict | None, product: dict | None) -> bool:
    return alert_mode(state, product) in MUTED_MODES


def in_stock_cap(state: dict | None, product: dict | None) -> int | None:
    """Max in-stock repeats; None means unlimited ("Keep alerting")."""
    return None if alert_mode(state, product) == ALERT_MODE_KEEP else IN_STOCK_ALERT_MAX


def should_check(state: dict, product: dict, now: float | None = None) -> bool:
    """Muted watches are only re-read every MUTED_CHECK_S (heartbeat freshness)."""
    if not is_muted(state, product):
        return True
    now = time.time() if now is None else now
    pstate = product_state(state, product)
    last = float(pstate.get("last_muted_check") or 0)
    if now - last >= MUTED_CHECK_S:
        pstate["last_muted_check"] = now
        return True
    return False


def _cb_pid(pid: str) -> str:
    """Product id as it fits in callback_data (64 bytes max)."""
    if len(pid.encode("utf-8")) <= 50:
        return pid
    return "#" + hashlib.sha1(pid.encode("utf-8")).hexdigest()[:12]


def _find_product(products: list[dict] | None, token: str) -> dict | None:
    for p in products or []:
        pid = str(p.get("id") or "")
        if pid and (pid == token or _cb_pid(pid) == token):
            return p
    return None


def _url_row(product: dict) -> list[dict]:
    return [{"text": button_label(product), "url": product.get("url") or PRODUCT_URL}]


def build_alert_keyboard(product: dict, mode: str | None = None) -> dict:
    """View product + the three alert-control buttons (or the chosen state)."""
    product = _prod(product)
    cid = _cb_pid(str(product.get("id") or "p"))
    rows = [_url_row(product)]
    if mode in MUTED_MODES:
        label = "✅ Bought — alerts off" if mode == ALERT_MODE_BOUGHT else "🚫 Not buying — alerts off"
        rows.append([
            {"text": label, "callback_data": f"rs|n|{cid}"},
            {"text": "↩️ Resume", "callback_data": f"rs|r|{cid}"},
        ])
        return {"inline_keyboard": rows}
    keep = "🔔 Keep alerting ✓" if mode == ALERT_MODE_KEEP else "🔔 Keep alerting"
    rows.append([
        {"text": "✅ Bought it", "callback_data": f"rs|b|{cid}"},
        {"text": "🚫 Not buying", "callback_data": f"rs|s|{cid}"},
    ])
    rows.append([{"text": keep, "callback_data": f"rs|k|{cid}"}])
    return {"inline_keyboard": rows}


def set_alert_mode(state: dict, product: dict, mode: str | None) -> None:
    """Change a watch's alert mode and drop alerts that should no longer go out."""
    pstate = product_state(state, product)
    pstate["alert_mode"] = mode
    pstate["alert_mode_ts"] = now_mx().isoformat()
    pid = product.get("id")
    if mode in MUTED_MODES:
        state["pending_events"] = [
            e for e in (state.get("pending_events") or [])
            if not (e.get("watch_id") == pid and e.get("kind") != "heartbeat")
        ]
    elif mode is None:
        # Fresh start: allow a new round of in-stock alerts right away.
        pstate["in_stock_alerts_sent"] = 0
        pstate["last_in_stock_alert_ts"] = None
        pstate["last_muted_check"] = 0
    log.info("Alert mode for %s -> %s", pid, mode or "default")
    save_state(state)


def set_poll_context(cfg: dict, state: dict, products: list[dict]) -> None:
    _poll_ctx.update(cfg=cfg, state=state, products=products)


def _chat_ok(chat_id) -> bool:
    creds = _tg_creds()
    return bool(creds) and str(chat_id) == str(creds[1])


def _answer_callback(cq_id: str, text: str) -> None:
    try:
        _tg_post(
            "answerCallbackQuery",
            {"callback_query_id": cq_id, "text": text[:190]},
            with_chat=False,
        )
    except Exception as exc:  # already answered by the other watcher, expired, ...
        log.info("answerCallbackQuery skipped: %s", exc)


def _edit_markup(chat_id, message_id, markup: dict) -> None:
    try:
        _tg_post(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": json.dumps(markup, ensure_ascii=False),
            },
            with_chat=False,
        )
    except Exception as exc:  # "message is not modified" when both watchers edit
        log.info("editMessageReplyMarkup skipped: %s", exc)


_CB_TOAST = {
    "b": "✅ Nice! No more alerts for {name}.",
    "s": "🚫 OK, alerts off for {name}.",
    "k": "🔔 OK, I'll keep alerting until you buy {name}.",
    "r": "↩️ Alerts are back on for {name}.",
    "n": "Alerts are off for {name}. Tap Resume to turn them back on.",
}
_CB_MODE = {"b": ALERT_MODE_BOUGHT, "s": ALERT_MODE_SKIP, "k": ALERT_MODE_KEEP, "r": None}


def handle_callback(state: dict, products: list[dict], cq: dict) -> None:
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not _chat_ok(chat_id):
        log.warning("Ignoring button tap from another chat")
        return
    parts = str(cq.get("data") or "").split("|", 2)
    if len(parts) != 3 or parts[0] != "rs" or parts[1] not in _CB_TOAST:
        _answer_callback(cq.get("id"), "Unknown button")
        return
    action, token = parts[1], parts[2]
    if token == TEST_CALLBACK_ID:
        # Buttons on a "[TEST]" card: confirm the tap without touching any watch.
        labels = {"b": "Bought it", "s": "Not buying", "k": "Keep alerting", "r": "Resume", "n": "status"}
        log.info("Test button tap received: %s", labels[action])
        _answer_callback(cq.get("id"), f"🧪 Test OK: “{labels[action]}” received. No watch was changed.")
        return
    product = _find_product(products, token)
    if not product:
        _answer_callback(cq.get("id"), "This watch is no longer in the config.")
        return
    name = heartbeat_display_name(product)
    if action != "n":
        set_alert_mode(state, product, _CB_MODE[action])
    _answer_callback(cq.get("id"), _CB_TOAST[action].format(name=name))
    if msg.get("message_id") and action != "n":
        _edit_markup(chat_id, msg["message_id"], build_alert_keyboard(product, _CB_MODE[action]))


def _mode_label(mode: str | None) -> str:
    return {
        ALERT_MODE_BOUGHT: "✅ bought, alerts off",
        ALERT_MODE_SKIP: "🚫 not buying, alerts off",
        ALERT_MODE_KEEP: "🔔 keep alerting",
    }.get(mode or "", "🔔 alerts on")


def build_status_text(state: dict, products: list[dict]) -> str:
    lines = ["📋 <b>Restockr status</b>"]
    bucket = state.get("products") or {}
    for p in products:
        ps = bucket.get(p.get("id") or "") or {}
        lines.append(
            f"{p.get('emoji') or '•'} <b>{htmlmod.escape(heartbeat_display_name(p))}</b> "
            f"(<code>{htmlmod.escape(str(p.get('id')))}</code>): "
            f"{htmlmod.escape(heartbeat_watch_status(p, ps))} · {_mode_label(ps.get('alert_mode'))}"
        )
    lines += ["", "/reset &lt;id|all&gt; turns alerts back on."]
    return "\n".join(lines)


def handle_command(state: dict, products: list[dict], msg: dict) -> str | None:
    """Apply a text command. Returns the reply text (sent only by one watcher)."""
    if not _chat_ok((msg.get("chat") or {}).get("id")):
        return None
    text = str(msg.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@", 1)[0].lower()
    arg = arg.strip()
    if cmd in ("/status", "/start", "/help"):
        return build_status_text(state, products)
    if cmd in ("/reset", "/unmute", "/resume"):
        if not arg:
            return "Usage: /reset &lt;id&gt; or /reset all\n\n" + build_status_text(state, products)
        targets = list(products) if arg.lower() == "all" else [
            p for p in products
            if str(p.get("id")).lower() == arg.lower()
            or heartbeat_display_name(p).lower() == arg.lower()
        ]
        if not targets:
            return f"No watch named <code>{htmlmod.escape(arg)}</code>.\n\n" + build_status_text(state, products)
        for p in targets:
            set_alert_mode(state, p, None)
        names = ", ".join(htmlmod.escape(heartbeat_display_name(p)) for p in targets)
        return f"↩️ Alerts are back on for {names}."
    return None


def poll_telegram(force: bool = False) -> int:
    """Read button taps and commands. Returns the number of new updates handled.

    Two watchers share one bot, so updates are NOT consumed right away: each
    watcher remembers the last update_id it handled (in its own state) and
    only confirms (deletes) updates first seen more than TG_UPDATE_RETAIN_S
    ago. Both watchers therefore apply every tap to their own state.
    """
    global _last_poll
    if TEST_MODE or not _poll_ctx.get("state"):
        return 0
    now_m = time.monotonic()
    if not force and now_m - _last_poll < TG_POLL_S:
        return 0
    _last_poll = now_m
    state = _poll_ctx["state"]
    products = _poll_ctx.get("products") or []
    fields = {
        "timeout": 0,
        "limit": 100,
        "allowed_updates": json.dumps(["callback_query", "message"]),
    }
    if state.get("tg_confirm_offset"):
        fields["offset"] = int(state["tg_confirm_offset"])
    try:
        data = _tg_post("getUpdates", fields, with_chat=False)
    except urllib.error.HTTPError as exc:
        log.info("getUpdates HTTP %s (another poll in flight?)", exc.code)
        return 0
    except Exception as exc:
        log.info("getUpdates failed: %s", exc)
        return 0
    if not data.get("ok"):
        log.info("getUpdates not ok: %s", data.get("description"))
        return 0
    last_id = int(state.get("tg_last_update_id") or 0)
    seen = state.setdefault("tg_seen", {})
    now = time.time()
    handled = 0
    for upd in data.get("result") or []:
        uid = int(upd.get("update_id") or 0)
        seen.setdefault(str(uid), now)
        if uid <= last_id:
            continue
        last_id = uid
        handled += 1
        try:
            if upd.get("callback_query"):
                handle_callback(state, products, upd["callback_query"])
            elif upd.get("message"):
                msg = upd["message"]
                reply = handle_command(state, products, msg)
                fresh = now - float(msg.get("date") or 0) <= TG_COMMAND_MAX_AGE_S
                if reply and TG_REPLY_COMMANDS and fresh:
                    tg_send(reply)
        except Exception:
            log.exception("Failed to handle Telegram update %s", uid)
    state["tg_last_update_id"] = last_id
    old = [int(k) for k, t in seen.items() if now - float(t) >= TG_UPDATE_RETAIN_S]
    if old:
        offset = max(old) + 1
        state["tg_confirm_offset"] = max(offset, int(state.get("tg_confirm_offset") or 0))
        state["tg_seen"] = {k: t for k, t in seen.items() if int(k) >= state["tg_confirm_offset"]}
    if handled or old:
        save_state(state)
    return handled


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
    if status == "queue":
        return "🚦 Waiting room open"
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
    if event_line and (status != "in_stock" or event_line == QUEUE_ENDED_LINE):
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
    if status == "in_stock":
        estado = "🟢 In stock, buy now!"
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
    if event_line and (status != "in_stock" or event_line == QUEUE_ENDED_LINE):
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
    if restock and status != "in_stock":
        if fecha:
            lines.append(f"📅 {_bold('Expected date:', markup)} {_esc(fecha, markup)}")
        else:
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
    reply_markup: dict | None = None,
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
        reply_markup or {"inline_keyboard": [[{"text": button_label(product), "url": url}]]},
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



def is_disney(product: dict | None) -> bool:
    return "disneystore." in ((product or {}).get("url") or "").lower()


_DISNEY_OOS_BTN = re.compile(r"sold\s*out|coming\s*soon|notify\s*me|unavailable|out\s*of\s*stock", re.I)


def disney_html_stock(html: str) -> str | None:
    """Stock status from Disney Store (Salesforce) server-rendered markup.

    Returns "in_stock", "out_of_stock", or None when the markers are missing.
    The page always ships a hidden ``product-oos-info d-none`` template and a
    ``data-soldOut-copy="Sold Out"`` attribute, so those alone mean nothing.
    """
    html = html or ""
    buttons = re.findall(
        r"(<button\b[^>]*\bid=[\"']add-to-cart[\"'][^>]*>)(.*?)</button>", html, re.I | re.S
    )
    avail_m = re.search(r"availability(?:&quot;|\")\s*:\s*(?:&quot;|\")([^\"&]+)", html, re.I)
    avail = (avail_m.group(1) if avail_m else "").lower()
    schema_m = re.search(
        r"itemprop=[\"']availability[\"'][^>]*?(?:href=[\"'][^\"']*?|>\s*(?:https?://schema\.org/)?)(\w+)",
        html,
        re.I,
    )
    schema = (schema_m.group(1) if schema_m else "").lower()
    if not buttons and not avail and not schema:
        return None
    btn_disabled = any(re.search(r"\sdisabled(?=[\s>=/])", tag, re.I) for tag, _ in buttons)
    btn_oos_text = any(
        _DISNEY_OOS_BTN.search(re.sub(r"<[^>]+>", " ", inner)) for _, inner in buttons
    )
    oos_visible = bool(re.search(r"class=[\"'](?![^\"']*\bd-none\b)[^\"']*\bproduct-oos-info(?![\w-])", html, re.I))
    avail_oos = any(k in avail for k in ("out_of_stock", "out of stock", "sold", "unavailable", "not_available"))
    avail_in = any(k in avail for k in ("in_stock", "in stock", "preorder", "pre_order", "backorder", "back_order"))
    schema_oos = schema in ("outofstock", "soldout", "discontinued")
    schema_in = schema in ("instock", "preorder", "backorder", "limitedavailability", "onlineonly")
    if btn_disabled or btn_oos_text or oos_visible:
        return "out_of_stock"
    if buttons and not avail_oos and not schema_oos:
        return "in_stock"
    if avail_oos or schema_oos:
        return "out_of_stock"
    if avail_in or schema_in:
        return None  # in-stock flag but no buy button: let the generic rules decide
    return None


def is_supply(product: dict | None) -> bool:
    return "openai.com/supply" in ((product or {}).get("url") or "").lower()


_SUPPLY_BTN = re.compile(
    r"(<button\b[^>]*data-analytics=[\"']supply-add-to-cart[\"'][^>]*>)(.*?)</button>",
    re.I | re.S,
)
_SUPPLY_PRODUCT_JSON = re.compile(
    r'\\?"product\\?"\s*:\s*\{\s*\\?"id\\?"\s*:\s*\\?"gid://shopify/Product/\d+\\?"\s*,'
    r'\s*\\?"handle\\?"\s*:\s*\\?"[^"\\]*\\?"\s*,\s*\\?"availableForSale\\?"\s*:\s*(true|false)',
    re.I,
)
_SUPPLY_OOS_LABEL = re.compile(r"restock\s*soon|out\s*of\s*stock|sold\s*out|coming\s*soon|notify\s*me", re.I)


def supply_html_stock(html: str) -> str | None:
    """Stock status for an OpenAI Supply Co. product page (Next.js + Shopify).

    Uses only the main product's own signals, so "Restock soon" text elsewhere
    on the page (related items, FAQ copy) cannot hide a restock:
    - the first ``data-analytics="supply-add-to-cart"`` button. The site sets
      ``data-supply-out-of-stock="true"`` only when every variant is
      unavailable. The server-rendered button is always ``disabled`` until the
      cart script loads, so ``disabled`` alone means nothing.
    - the main product's Shopify ``availableForSale`` flag in the page data.
    Either positive signal counts as in stock (a missed restock is worse than a
    false alarm). Returns None when neither marker is found.
    """
    html = html or ""
    m = _SUPPLY_BTN.search(html)
    j = _SUPPLY_PRODUCT_JSON.search(html)
    if not m and not j:
        return None
    json_avail = j.group(1).lower() == "true" if j else None
    if json_avail:
        return "in_stock"
    if m:
        tag, inner = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        oos_attr = bool(re.search(r'data-supply-out-of-stock\s*=\s*["\']true["\']', tag, re.I))
        if not oos_attr and not _SUPPLY_OOS_LABEL.search(inner):
            return "in_stock"
    return "out_of_stock"


def disney_variation_status(data: dict | None) -> str | None:
    """Stock from Disney's Product-Variation JSON (available + readyToOrder)."""
    if not isinstance(data, dict):
        return None
    prod = data.get("product")
    if not isinstance(prod, dict) or "available" not in prod:
        return None
    if prod.get("isForceSellOutProduct"):
        return "out_of_stock"
    if prod.get("available") and prod.get("readyToOrder", True):
        return "in_stock"
    return "out_of_stock"


def disney_variation_url(product: dict | None) -> str | None:
    url = (product or {}).get("url") or ""
    m = re.search(r"-(\d{6,})\.html", url)
    if not is_disney(product) or not m:
        return None
    host = urllib.parse.urlparse(url).netloc or "www.disneystore.com"
    return (
        f"https://{host}/on/demandware.store/Sites-shopDisney-Site/default/"
        f"Product-Variation?pid={m.group(1)}&quantity=1"
    )


def looks_like_queue(title: str, html: str, final_url: str = "") -> bool:
    """True if we landed on a Queue-it style virtual waiting room."""
    host = urllib.parse.urlparse(final_url or "").netloc.lower()
    if host.startswith("queue.") or host.endswith("queue-it.net"):
        return True
    html = html or ""
    if re.search(r"\bid=[\"']add-to-cart[\"']", html, re.I):
        return False
    low = html[:300000].lower()
    t = (title or "").lower()
    if "queue-it" in low or "queueit" in low:
        return True
    return any(k in t for k in ("waiting room", "virtual queue", "you are now in line", "queue-it"))


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

    if is_supply(product):
        supply = supply_html_stock(html)
        if supply is not None:
            status = supply
            out_of_stock = supply == "out_of_stock"
            enabled_add = supply == "in_stock"

    if is_disney(product):
        disney = disney_html_stock(html)
        if disney is not None:
            status = disney
            out_of_stock = disney == "out_of_stock"
            enabled_add = disney == "in_stock"

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
    if is_restock(product) and looks_like_queue(title, html):
        return {"status": "queue", "product_ok": True}
    visible = _visible_from_html(html)
    info = evaluate_page(title, html, visible, product)
    return apply_disney_variation(product, info, _fetch_json_urllib)


def _fetch_json_urllib(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": CHROME_UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        final = resp.geturl()
        body = resp.read().decode("utf-8", errors="replace")
    return final, body


def apply_disney_variation(product: dict, info: dict, fetch) -> dict:
    """Confirm Disney stock with the Product-Variation JSON.

    ``fetch(url) -> (final_url, body)``. If the JSON is redirected to the
    waiting room, report the queue. Errors keep the HTML result.
    """
    vurl = disney_variation_url(product) if is_restock(product) else None
    if not vurl or info.get("status") == "queue":
        return info
    try:
        final, body = fetch(vurl)
    except Exception as exc:
        log.info("Disney variation check skipped (%s)", exc)
        return info
    if looks_like_queue("", body if not body.lstrip().startswith("{") else "", final):
        return {"status": "queue", "product_ok": True}
    try:
        data = json.loads(body)
    except Exception:
        return info
    st = disney_variation_status(data)
    if st and st != info.get("status"):
        log.info(
            "Disney variation JSON says %s (HTML said %s); using JSON",
            st,
            info.get("status"),
        )
    if st:
        info = dict(info)
        info["status"] = st
        info["out_of_stock"] = st == "out_of_stock"
        info["enabled_add"] = st == "in_stock"
        info["variation_status"] = st
    return info


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
    reply_markup: dict | str | None = None,
) -> dict | None:
    """Queue an alert. Not marked delivered until each channel succeeds.

    If an event of the same watch+kind is already pending, its payload is
    updated to the latest values (coalesced). A channel that already
    delivered the earlier copy is set back to pending: this is a new alert
    (e.g. the next in-stock repeat) and must go out again even while another
    channel (ntfy 429) is still retrying. Channels still retrying keep their
    backoff.
    """
    if TEST_MODE:
        return None
    product = product or {}
    if kind != "heartbeat" and is_muted(state, product):
        log.info("Muted (%s): not sending %s for %s", alert_mode(state, product), kind, product.get("id"))
        return None
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
        "reply_markup": reply_markup,
        "alert_mode": alert_mode(state, product),
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
                for key in ("event_line", "info", "pstate", "product", "text", "title", "priority", "tags", "use_card", "is_test", "reply_markup", "alert_mode"):
                    existing[key] = event[key]
                existing["updated_ts"] = event["created_ts"]
                chans = existing.setdefault("channels", {})
                for name, fresh in event["channels"].items():
                    ch = chans.get(name)
                    if not ch or ch.get("status") == "sent" or fresh.get("status") == "skipped":
                        chans[name] = dict(fresh)
                    elif int(ch.get("attempts") or 0) >= DELIVERY_MAX_ATTEMPTS:
                        # Exhausted: give the new alert a fresh set of tries.
                        chans[name] = dict(fresh)
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
            markup = None
            if event.get("kind") in CONTROL_KINDS and is_restock(product) and product.get("id"):
                markup = build_alert_keyboard(product, event.get("alert_mode"))
            return bool(
                tg_send_card(
                    info,
                    pstate,
                    product=product,
                    is_test=bool(event.get("is_test")),
                    event_line=event.get("event_line"),
                    reply_markup=markup,
                )
            )
        return bool(
            tg_send(
                event.get("text") or event.get("event_line") or "",
                reply_markup=event.get("reply_markup"),
            )
        )
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
        title = html_to_plain(event.get("title") or event.get("event_line") or "Restockr")
        ok, _status = ntfy(
            cfg,
            title=title,
            message=html_to_plain(event.get("text") or title),
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
        def _done(ch: dict) -> bool:
            return ch.get("status") in ("sent", "skipped") or (
                ch.get("status") == "failed" and int(ch.get("attempts") or 0) >= DELIVERY_MAX_ATTEMPTS
            )
        if all(_done(channels.get(n) or {}) for n in ("telegram", "ntfy")):
            if any((channels.get(n) or {}).get("status") == "failed" for n in ("telegram", "ntfy")):
                log.warning("Giving up on %s/%s for a failed channel after %s attempts",
                            event.get("watch_id"), event.get("kind"), DELIVERY_MAX_ATTEMPTS)
                changed = True
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





def html_to_plain(text: str | None) -> str:
    """Strip a small subset of HTML so ntfy gets readable plain text."""
    if not text:
        return ""
    out = re.sub(r'<a\s+href=["\']([^"\']+)["\'][^>]*>.*?</a>', r"\1", text, flags=re.I | re.S)
    out = re.sub(r"<br\s*/?>", "\n", out, flags=re.I)
    out = re.sub(r"</?b>", "", out, flags=re.I)
    out = re.sub(r"<[^>]+>", "", out)
    return htmlmod.unescape(out).strip()


def format_display_time(dt: datetime, *, with_date: bool = False) -> str:
    """Format a timestamp in America/Asuncion, always labeled PYT."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    local = dt.astimezone(DISPLAY_TZ)
    clock = local.strftime("%H:%M")
    if with_date:
        return f"{local.strftime('%b')} {local.day}, {clock} {DISPLAY_TZ_LABEL}"
    return f"{clock} {DISPLAY_TZ_LABEL}"


def heartbeat_display_name(product: dict | None) -> str:
    product = product or {}
    raw = product.get("short_name") or product.get("name") or product.get("id") or "Watch"
    name = re.sub(r"^(the|la|el|los|las)\s+", "", str(raw).strip(), flags=re.I).strip()
    return name or str(product.get("name") or product.get("id") or "Watch")


def heartbeat_watch_status(product: dict | None, pstate: dict | None = None) -> str:
    """Compact status line for the daily heartbeat."""
    product = product or {}
    pstate = pstate or {}
    if pstate.get("alert_mode") == ALERT_MODE_BOUGHT:
        return "✅ Bought (alerts off)"
    if pstate.get("alert_mode") == ALERT_MODE_SKIP:
        return "🚫 Not buying (alerts off)"
    status = pstate.get("last_status")
    if is_restock(product) and pstate.get("queue_active"):
        status = "queue"
    if is_restock(product):
        if status == "in_stock":
            return "🟢 In stock"
        if status == "queue":
            return "🚦 Waiting room is open"
        if status == "out_of_stock":
            return "⏳ Not in stock yet"
        return "👀 Watching"
    if status == "triggered":
        return "🟢 Condition met"
    if status == "changed":
        return "🔔 Content changed"
    return "👀 Watching"


def _heartbeat_remind_at(product: dict) -> datetime | None:
    value = product.get("remind_at")
    if isinstance(value, datetime):
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value)
            return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt
        except ValueError:
            pass
    try:
        return parse_remind_at(product)
    except ValueError:
        return None


def heartbeat_button_label(product: dict | None, words: int = 1) -> str:
    """Short inline-keyboard label, e.g. '🎮 Open Chromatic'."""
    product = product or {}
    emoji = product.get("emoji") or "🔗"
    name = heartbeat_display_name(product)
    parts = name.split()
    short = " ".join(parts[: max(1, words)]) if parts else "page"
    return f"{emoji} Open {short}"


def _heartbeat_products(
    cfg: dict | None,
    fallback_product: dict | None = None,
) -> list[dict]:
    products = list((cfg or {}).get("products") or [])
    if not products and fallback_product:
        products = [fallback_product]
    return products


def build_heartbeat_keyboard(
    cfg: dict | None,
    fallback_product: dict | None = None,
) -> dict | None:
    """Inline URL buttons, up to two per row, one per watched page."""
    rows: list[list[dict]] = []
    row: list[dict] = []
    products = [p for p in _heartbeat_products(cfg, fallback_product) if p.get("url")]
    labels = [heartbeat_button_label(p) for p in products]
    # Two watches can share a first word ("Chromatic Codex" / "Chromatic Case"):
    # use two words for those so the buttons stay distinguishable.
    for i, product in enumerate(products):
        if labels.count(labels[i]) > 1 or any(
            heartbeat_display_name(o).split()[:1] == heartbeat_display_name(product).split()[:1]
            for o in products if o is not product
        ):
            labels[i] = heartbeat_button_label(product, words=2)
    for product, label in zip(products, labels):
        url = product.get("url")
        row.append({"text": label, "url": str(url)})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        return None
    return {"inline_keyboard": rows}


def _heartbeat_product_lines(product: dict, pstate: dict | None = None) -> list[str]:
    """One heartbeat block for a watch, starting with a blank line."""
    pstate = pstate or {}
    emoji = product.get("emoji") or "•"
    name = heartbeat_display_name(product)
    status = heartbeat_watch_status(product, pstate)
    lines = [
        "",
        f"{emoji} <b>{htmlmod.escape(name)}</b>",
        f"📦 <b>Status:</b> {htmlmod.escape(status)}",
    ]
    if is_restock(product):
        expected = format_date(
            pstate.get("last_expected_restock") or product.get("expected_date")
        )
        if expected:
            lines.append(f"📅 <b>Expected date:</b> {htmlmod.escape(expected)}")
    price = pstate.get("product_price")
    if price:
        lines.append(f"💵 <b>Price:</b> {htmlmod.escape(str(price))}")
    return lines


def _render_heartbeat(header: list[str], blocks: list[list[str]], omitted: int) -> str:
    lines = list(header)
    for block in blocks:
        lines.extend(block)
    if omitted:
        lines += ["", f"+{omitted} more"]
    lines += ["", "I'll ping you the moment something changes."]
    return "\n".join(lines)


def build_heartbeat_text(
    cfg: dict | None,
    state: dict | None,
    *,
    now: datetime | None = None,
    fallback_product: dict | None = None,
) -> str:
    """Daily heartbeat in HTML (Telegram parse_mode=HTML). Uses saved state only.

    Product links are sent as inline keyboard buttons, not as in-text View links.
    The same-day reminder is sent separately and is not listed here. If the
    message would exceed Telegram's text limit, trailing watches are omitted
    and replaced with '+N more'.
    """
    now = now or now_mx()
    state = state or {}
    products = _heartbeat_products(cfg, fallback_product)
    n = len(products)
    watch_word = "watch" if n == 1 else "watches"
    header = [
        "🟢 <b>Restockr is running</b>",
        f"🕐 Last check: {format_display_time(now)} · {n} {watch_word} active",
    ]
    bucket = state.get("products") or {}
    blocks = [
        _heartbeat_product_lines(product, bucket.get(product.get("id") or "") or {})
        for product in products
    ]
    visible = list(blocks)
    omitted = 0
    text = _render_heartbeat(header, visible, omitted)
    while len(text) > TELEGRAM_TEXT_MAX and visible:
        visible.pop()
        omitted += 1
        text = _render_heartbeat(header, visible, omitted)
    if len(text) > TELEGRAM_TEXT_MAX:
        text = text[: TELEGRAM_TEXT_MAX - 1].rstrip() + "…"
    return text


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
    # The box backup sets "daily_notices": false in its local config so the
    # reminder and the daily heartbeat come from the cloud watcher only (one
    # copy each). Stock alerts are still sent by both watchers.
    notices = (cfg or {}).get("daily_notices", True) is not False
    if remind_at and n >= remind_at and notices:
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
        notices
        and product.get("primary")
        and n >= heartbeat_at
        and today not in state.get("heartbeat_dates", [])
        and status != "in_stock"
    ):
        text = build_heartbeat_text(cfg, state, now=n, fallback_product=product)
        emit_alert(
            cfg,
            state,
            product,
            kind="heartbeat",
            use_card=False,
            title="🟢 Restockr is running",
            text=text,
            event_line="🟢 Restockr is running",
            reply_markup=build_heartbeat_keyboard(cfg, fallback_product=product),
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
        cap = in_stock_cap(state, product)
        if due and (cap is None or sent < cap):
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


def handle_queue(cfg: dict, state: dict, product: dict, info: dict) -> None:
    """The page redirected to the virtual waiting room."""
    pstate = product_state(state, product)
    pstate["consecutive_failures"] = 0
    pstate["queue_clear_streak"] = 0
    log.info("CHECK OK  product=%s  status=QUEUE (virtual waiting room is ON)", product.get("id"))
    now = time.time()
    if not pstate.get("queue_active"):
        pstate["queue_active"] = True
        pstate["queue_alerts_sent"] = 0
        pstate["queue_alert_ts"] = 0
        pstate["queue_since"] = now_mx().isoformat()
    sent = int(pstate.get("queue_alerts_sent") or 0)
    last = float(pstate.get("queue_alert_ts") or 0)
    if sent < QUEUE_ALERT_MAX and now - last >= QUEUE_ALERT_GAP_S:
        emit_alert(
            cfg,
            state,
            product,
            kind="queue",
            info={**info, "status": "queue"},
            pstate=pstate,
            event_line=QUEUE_OPEN_LINE,
            priority=5,
            tags=["rotating_light", "shopping"],
        )
        pstate["queue_alerts_sent"] = sent + 1
        pstate["queue_alert_ts"] = now
    save_state(state)


def note_page_reachable(cfg: dict, state: dict, product: dict, info: dict) -> None:
    """The real product page loaded. End an active waiting room once confirmed."""
    pstate = product_state(state, product)
    if not pstate.get("queue_active"):
        pstate["queue_clear_streak"] = 0
        return
    streak = int(pstate.get("queue_clear_streak") or 0) + 1
    pstate["queue_clear_streak"] = streak
    # In stock ends it right away; otherwise wait for a second reachable read.
    if streak < QUEUE_END_CONFIRM and info.get("status") != "in_stock":
        log.info("Waiting room may be over (%s/%s reachable reads)", streak, QUEUE_END_CONFIRM)
        save_state(state)
        return
    log.info("Waiting room is over for %s (page status=%s)", product.get("id"), info.get("status"))
    emit_alert(
        cfg,
        state,
        product,
        kind="queue_ended",
        info=info,
        pstate=pstate,
        event_line=QUEUE_ENDED_LINE,
        priority=5,
        tags=["white_check_mark", "shopping"],
    )
    pstate["queue_active"] = False
    pstate["queue_alerts_sent"] = 0
    pstate["queue_alert_ts"] = 0
    pstate["queue_clear_streak"] = 0
    pstate["queue_ended_at"] = now_mx().isoformat()
    save_state(state)


def handle_check(cfg: dict, state: dict, product: dict, info: dict) -> None:
    """Route one successful check: waiting room, waiting room over, stock."""
    if info.get("status") == "queue":
        handle_queue(cfg, state, product, info)
        return
    if is_restock(product):
        note_page_reachable(cfg, state, product, info)
    handle_result(cfg, state, product, info)


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


def sleep_jitter(
    extra: float = 0.0,
    in_stock_repeat: bool = False,
    products=None,
    now=None,
) -> None:
    low, high = check_interval_range(
        now=now, products=products, in_stock_repeat=in_stock_repeat
    )
    base = low if low == high else random.uniform(low, high)
    if extra and is_reminder_day(now, products):
        # Failure backoff can reach 10 minutes. On a restock day keep it short
        # so a blocked or slow watch cannot hide the restock for long.
        extra = min(extra, float(REMINDER_DAY_BACKOFF_CAP_S))
    total = base + extra
    log.info("sleeping %.1fs", total)
    end = time.monotonic() + total
    wall_end = time.time() + total
    # Guard against monotonic clock jumps (box suspend/restore): also stop on wall clock.
    while time.monotonic() < end and time.time() < wall_end and not _stop:
        try:
            poll_telegram()
        except Exception:
            log.exception("Telegram poll failed")
        time.sleep(max(0.0, min(1.0, end - time.monotonic(), wall_end - time.time())))


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
        if info.get("status") != "in_stock" or is_muted(state, p):
            continue
        cap = in_stock_cap(state, p)
        if cap is None or int(pstate.get("in_stock_alerts_sent") or 0) < cap:
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
            set_poll_context(cfg, state, products)
            poll_telegram(force=True)
            flush_pending(cfg, state, products)
            extra = 0.0
            last_results: dict = {}
            for product in products:
                if _stop:
                    break
                if not should_check(state, product):
                    continue
                try:
                    info = check_page(product)
                    handle_check(cfg, state, product, info)
                    last_results[product["id"]] = info
                except Exception as exc:
                    extra = max(extra, handle_failure(cfg, state, product, f"{exc}"))
                    last_results[product["id"]] = None
            if _stop:
                break
            sleep_jitter(
                extra=extra,
                in_stock_repeat=any_in_stock_repeat(state, products, last_results),
                products=products,
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
