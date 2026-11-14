#!/usr/bin/env python3
"""Restockr in the cloud (GitHub Actions).

Checks each product page with plain HTTPS requests (curl_cffi, which looks like
a real Chrome to Cloudflare/Akamai) every 60-120 s, for RUN_MINUTES, then exits
so the next scheduled run takes over. Alerts: Telegram (Restockr bot) + ntfy.
No AI / no tokens at runtime.

Secrets come from environment variables (GitHub Actions secrets):
  TG_TOKEN, TG_CHAT, NTFY_TOPIC
Optional: RUN_MINUTES (default 340), MODE=watch|test
"""
from __future__ import annotations

import html as htmlmod
import json
import os
import shutil
import sys
import time
import traceback
from urllib.parse import urlparse

from curl_cffi import requests as cr

import restockr_core as core

log = core.log
IMPERSONATE = "chrome"
_sessions: dict[str, cr.Session] = {}


# ---------- secrets from env (never printed) ----------
def _env_creds():
    token = (os.environ.get("TG_TOKEN") or "").strip()
    chat = (os.environ.get("TG_CHAT") or "").strip()
    if not token or not chat:
        return None
    return token, chat


core._tg_creds = _env_creds


def load_config() -> dict:
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    cfg = json.loads(core.CONFIG_PATH.read_text())
    cfg["topic"] = topic or "restockr-no-topic"
    cfg["_ntfy_enabled"] = bool(topic)
    # reuse core normalization without re-reading the file
    tmp = core.CONFIG_PATH.with_name("config.runtime.json")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False))
    orig = core.CONFIG_PATH
    core.CONFIG_PATH = tmp
    try:
        out = core.load_config()
    finally:
        core.CONFIG_PATH = orig
        tmp.unlink(missing_ok=True)
    out["_ntfy_enabled"] = bool(topic)
    return out


# ---------- page fetch without Chrome ----------
def _session(pid: str) -> cr.Session:
    s = _sessions.get(pid)
    if s is None:
        s = cr.Session(impersonate=IMPERSONATE)
        _sessions[pid] = s
    return s


def _reset_session(pid: str) -> None:
    s = _sessions.pop(pid, None)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass


def check_page(product: dict | None = None, _context=None) -> dict:
    product = core._prod(product)
    pid = product.get("id") or "p"
    url = product.get("url") or core.PRODUCT_URL
    try:
        r = _session(pid).get(
            url,
            timeout=30,
            allow_redirects=True,
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )
    except Exception as exc:
        _reset_session(pid)
        raise RuntimeError(f"HTTP error ({pid}): {exc}") from exc
    final_host = urlparse(str(r.url)).netloc.lower()
    # Disney/Queue-it: when the virtual waiting room is ON we end on queue.* host
    if final_host.startswith("queue."):
        if core.is_restock(product):
            return {"status": "queue", "queue_url": str(r.url), "product_ok": True}
        raise RuntimeError(f"Waiting-room page ({pid})")
    if r.status_code >= 400:
        _reset_session(pid)
        raise RuntimeError(f"HTTP {r.status_code} ({pid})")
    html = r.text or ""
    title = core._title_from_html(html)
    visible = core._visible_from_html(html)
    try:
        return core.evaluate_page(title, html, visible, product)
    except Exception:
        _reset_session(pid)
        raise


core.check_page = check_page


# ---------- ntfy only if topic configured ----------
_orig_ntfy = core.ntfy


def _ntfy(cfg, **kw):
    if not cfg.get("_ntfy_enabled"):
        return False, 0
    return _orig_ntfy(cfg, **kw)


core.ntfy = _ntfy


# ---------- Disney virtual queue alert ----------
def handle_queue(cfg: dict, state: dict, product: dict, info: dict) -> None:
    pstate = core.product_state(state, product)
    pstate["consecutive_failures"] = 0
    log.info("CHECK OK  product=%s  status=QUEUE (virtual waiting room is ON)", product.get("id"))
    now = time.time()
    last = float(pstate.get("queue_alert_ts") or 0)
    sent = int(pstate.get("queue_alerts_sent") or 0)
    if not pstate.get("queue_active"):
        sent = 0
    if sent < 3 and now - last >= 180:
        line = "🚦 <b>The virtual waiting room is open! Join now</b>"
        core.ntfy_card(cfg, product, pstate=pstate, event_line=line, priority=5,
                       tags=["rotating_light", "shopping"])
        core.tg_send_card(state=pstate, product=product, event_line=line)
        pstate["queue_alerts_sent"] = sent + 1
        pstate["queue_alert_ts"] = now
    pstate["queue_active"] = True
    core.save_state(state)


def clear_queue_flag(state: dict, product: dict) -> None:
    pstate = core.product_state(state, product)
    if pstate.get("queue_active"):
        pstate["queue_active"] = False
        pstate["queue_alerts_sent"] = 0


# ---------- summary (intro / test) ----------
def summary_text(products, results) -> str:
    lines = ["☁️ <b>Restockr cloud watcher: active</b>", ""]
    for p in products:
        info = results.get(p["id"])
        name = htmlmod.escape(p.get("name") or p["id"])
        if info is None:
            lines.append(f"{p.get('emoji','•')} {name}: ⚠️ could not read the page")
        elif info.get("status") == "queue":
            lines.append(f"{p.get('emoji','•')} {name}: 🚦 virtual waiting room is open")
        elif not core.is_restock(p):
            st = core.status_label(info.get("status"), None)
            extra = f" — {htmlmod.escape(info.get('snippet'))}" if info.get("snippet") else ""
            lines.append(f"{p.get('emoji','•')} {name}: {st}{extra}")
        else:
            st = core.status_label(info.get("status"), info.get("expected"))
            fecha = core.format_date(info.get("expected"))
            extra = f" (expected: {fecha})" if fecha else ""
            lines.append(f"{p.get('emoji','•')} {name}: {st}{extra}")
    lines += ["", "Checking every 1–2 minutes, independently of this computer."]
    return "\n".join(lines)


def run_once(cfg, state, products):
    results: dict = {}
    extra = 0.0
    for product in products:
        try:
            info = check_page(product)
            if info.get("status") == "queue":
                handle_queue(cfg, state, product, info)
            else:
                clear_queue_flag(state, product)
                core.handle_result(cfg, state, product, info)
            results[product["id"]] = info
        except Exception as exc:
            extra = max(extra, core.handle_failure(cfg, state, product, f"{exc}"))
            results[product["id"]] = None
    return results, extra


def main() -> int:
    core.setup_logging()
    if not _env_creds():
        log.error("Missing TG_TOKEN / TG_CHAT secrets")
        return 2
    if not core.STATE_PATH.exists():
        seed = core.DIR / "state.seed.json"
        if seed.exists():
            shutil.copy(seed, core.STATE_PATH)
    cfg = load_config()
    products = cfg["products"]
    state = core.load_state(products)
    mode = (os.environ.get("MODE") or "watch").strip().lower()
    run_minutes = float(os.environ.get("RUN_MINUTES") or 340)
    deadline = time.monotonic() + run_minutes * 60
    log.info("Cloud watcher start mode=%s run_minutes=%s products=%s",
             mode, run_minutes, ",".join(p["id"] for p in products))

    if core.now_mx() >= core.END_AT:
        log.info("Past end date; nothing to do.")
        return 0

    if mode == "dry":
        for product in products:
            try:
                info = check_page(product)
                log.info(
                    "DRY OK  id=%s type=%s status=%s expected=%s price=%s triggered=%s snippet=%s",
                    product.get("id"),
                    core.entry_type(product),
                    (info or {}).get("status"),
                    (info or {}).get("expected"),
                    (info or {}).get("price"),
                    (info or {}).get("triggered"),
                    ((info or {}).get("snippet") or "")[:80],
                )
            except Exception as exc:
                log.info("DRY FAIL  id=%s  error=%s", product.get("id"), exc)
        return 0

    results, extra = run_once(cfg, state, products)
    if mode == "test" or not state.get("cloud_intro_sent"):
        text = summary_text(products, results)
        core.tg_send(text)
        core.ntfy(cfg, title="☁️ Restockr cloud watcher: active",
                  message=htmlmod.unescape(text.replace("<b>", "").replace("</b>", "")),
                  priority=3, tags=["cloud"])
        state["cloud_intro_sent"] = True
        # The intro already proves it's alive: skip today's heartbeat if it's past 12:00 PY
        n = core.now_mx()
        hb = n.replace(hour=core.HEARTBEAT_HOUR, minute=core.HEARTBEAT_MINUTE, second=0, microsecond=0)
        if mode != "test" and n >= hb and n.date().isoformat() not in state.get("heartbeat_dates", []):
            state.setdefault("heartbeat_dates", []).append(n.date().isoformat())
        core.save_state(state)
    if mode == "test":
        return 0

    while time.monotonic() < deadline and core.now_mx() < core.END_AT:
        core.sleep_jitter(extra=extra,
                          in_stock_repeat=core.any_in_stock_repeat(state, products, results))
        if time.monotonic() >= deadline:
            break
        results, extra = run_once(cfg, state, products)
    log.info("Run window finished; next scheduled run continues.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.error("Fatal:\n%s", traceback.format_exc())
        sys.exit(1)
