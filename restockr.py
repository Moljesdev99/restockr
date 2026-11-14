#!/usr/bin/env python3
"""Restockr in the cloud (GitHub Actions).

Checks each watched page with plain HTTPS requests (curl_cffi, which looks like
a real Chrome to Cloudflare/Akamai) every 60-120 s, for RUN_MINUTES, then exits
so the next scheduled run takes over. Alerts: Telegram (Restockr bot) + ntfy.
No AI / no tokens at runtime.

Secrets come from environment variables (GitHub Actions secrets):
  TG_TOKEN, TG_CHAT, NTFY_TOPIC
Optional: RUN_MINUTES (default 340), MODE=watch|test|dry
  GITHUB_TOKEN / GITHUB_REPOSITORY / GITHUB_REF_NAME for mid-run config reload
"""
from __future__ import annotations

import html as htmlmod
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from curl_cffi import requests as cr

import restockr_core as core

log = core.log
IMPERSONATE = "chrome"
_sessions: dict[str, cr.Session] = {}
_config_mtime: float | None = None
_last_remote_reload = 0.0


# ---------- secrets from env (never printed) ----------
def _env_creds():
    token = (os.environ.get("TG_TOKEN") or "").strip()
    chat = (os.environ.get("TG_CHAT") or "").strip()
    if not token or not chat:
        return None
    return token, chat


core._tg_creds = _env_creds


def _with_topic(data: dict) -> dict:
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    data = dict(data)
    data["topic"] = topic or "restockr-no-topic"
    data["_ntfy_enabled"] = bool(topic)
    return data


def load_config() -> dict:
    raw = json.loads(core.CONFIG_PATH.read_text())
    out = core.parse_config(_with_topic(raw), require_topic=False)
    out["_ntfy_enabled"] = bool((os.environ.get("NTFY_TOPIC") or "").strip())
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
        core.emit_alert(
            cfg,
            state,
            product,
            kind="queue",
            info=info,
            pstate=pstate,
            event_line=line,
            priority=5,
            tags=["rotating_light", "shopping"],
        )
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
            extra = f" — {htmlmod.escape(str(info.get('snippet')))}" if info.get("snippet") else ""
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
    core.flush_pending(cfg, state, products)
    return results, extra


# ---------- config hot-reload ----------
def _git_ref() -> str:
    ref = os.environ.get("GITHUB_REF_NAME") or os.environ.get("GITHUB_REF") or "main"
    if ref.startswith("refs/"):
        ref = ref.split("/")[-1]
    return ref or "main"


def fetch_remote_config_text() -> str | None:
    """Latest config.json from the repo, without printing tokens."""
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    ref = _git_ref()
    if repo and token:
        url = (
            f"https://api.github.com/repos/{repo}/contents/config.json"
            f"?ref={urllib.parse.quote(ref)}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "restockr-watcher",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:
            log.warning("Remote config API fetch failed: %s", exc)
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin", ref],
            cwd=str(core.DIR),
            capture_output=True,
            timeout=30,
            check=False,
        )
        if fetch.returncode == 0:
            show = subprocess.run(
                ["git", "show", f"origin/{ref}:config.json"],
                cwd=str(core.DIR),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if show.returncode == 0 and show.stdout.strip():
                return show.stdout
    except Exception as exc:
        log.warning("Remote config git fetch failed: %s", exc)
    return None


def _try_parse_config_text(text: str, current: dict) -> dict | None:
    try:
        data = json.loads(text)
        parsed = core.parse_config(_with_topic(data), require_topic=False)
        parsed["_ntfy_enabled"] = current.get("_ntfy_enabled", False)
        parsed["topic"] = current.get("topic") or parsed.get("topic")
        return parsed
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("Ignoring invalid reloaded config: %s", exc)
        return None


def maybe_reload_config(cfg: dict, state: dict) -> dict:
    """Reload config.json from disk or the GitHub repo every few minutes."""
    global _config_mtime, _last_remote_reload
    now = time.monotonic()
    updated = None

    path = core.CONFIG_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime and _config_mtime and mtime > _config_mtime:
        try:
            updated = _try_parse_config_text(path.read_text(), cfg)
            if updated:
                log.info("Reloaded config.json from disk")
        except Exception as exc:
            log.warning("Disk config reload failed: %s", exc)
    if mtime:
        _config_mtime = mtime

    if now - _last_remote_reload >= core.CONFIG_RELOAD_S:
        _last_remote_reload = now
        remote = fetch_remote_config_text()
        if remote:
            parsed = _try_parse_config_text(remote, cfg)
            if parsed:
                updated = parsed
                log.info("Reloaded config.json from the repository")
                try:
                    path.write_text(remote if remote.endswith("\n") else remote + "\n")
                    _config_mtime = path.stat().st_mtime
                except Exception:
                    pass

    if not updated:
        return cfg
    return core.apply_new_config(cfg, updated, state)


def run_test_mode(cfg: dict) -> int:
    """Check pages and send one summary. No regular alerts, no live state I/O."""
    from pathlib import Path as _Path

    core.TEST_MODE = True
    fd, tmp = tempfile.mkstemp(prefix="restockr-test-state-", suffix=".json")
    os.close(fd)
    original = core.STATE_PATH
    core.STATE_PATH = _Path(tmp)
    try:
        products = cfg["products"]
        results: dict = {}
        for product in products:
            try:
                results[product["id"]] = check_page(product)
            except Exception as exc:
                log.info("TEST FAIL id=%s error=%s", product.get("id"), exc)
                results[product["id"]] = None
        text = summary_text(products, results)
        core.TEST_MODE = False  # allow the single summary through
        core.tg_send(text)
        core.ntfy(
            cfg,
            title="☁️ Restockr cloud watcher: active",
            message=htmlmod.unescape(text.replace("<b>", "").replace("</b>", "")),
            priority=3,
            tags=["cloud"],
        )
        return 0
    finally:
        core.TEST_MODE = False
        core.STATE_PATH = original
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main() -> int:
    core.setup_logging()
    signal.signal(signal.SIGTERM, core.on_signal)
    signal.signal(signal.SIGINT, core.on_signal)
    mode = (os.environ.get("MODE") or "watch").strip().lower()
    if mode != "dry" and not _env_creds():
        log.error("Missing TG_TOKEN / TG_CHAT secrets")
        return 2
    cfg = load_config()
    try:
        global _config_mtime
        _config_mtime = core.CONFIG_PATH.stat().st_mtime
    except OSError:
        pass

    if mode == "test":
        log.info("Test mode: summary only, live state will not be used")
        return run_test_mode(cfg)

    if mode != "dry" and not core.STATE_PATH.exists():
        seed = core.DIR / "state.seed.json"
        if seed.exists():
            shutil.copy(seed, core.STATE_PATH)

    products = cfg["products"]
    state = core.default_state() if mode == "dry" else core.load_state(products)
    run_minutes = float(os.environ.get("RUN_MINUTES") or 340)
    deadline = time.monotonic() + run_minutes * 60
    log.info(
        "Cloud watcher start mode=%s run_minutes=%s products=%s",
        mode,
        run_minutes,
        ",".join(p["id"] for p in products),
    )

    if mode == "dry":
        core.TEST_MODE = True
        try:
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
        finally:
            core.TEST_MODE = False
        return 0

    try:
        core.flush_pending(cfg, state, products)
        results, extra = run_once(cfg, state, products)
        if not state.get("cloud_intro_sent"):
            text = summary_text(products, results)
            core.tg_send(text)
            core.ntfy(
                cfg,
                title="☁️ Restockr cloud watcher: active",
                message=htmlmod.unescape(text.replace("<b>", "").replace("</b>", "")),
                priority=3,
                tags=["cloud"],
            )
            state["cloud_intro_sent"] = True
            n = core.now_mx()
            hb = n.replace(
                hour=core.HEARTBEAT_HOUR, minute=core.HEARTBEAT_MINUTE, second=0, microsecond=0
            )
            if n >= hb and n.date().isoformat() not in state.get("heartbeat_dates", []):
                state.setdefault("heartbeat_dates", []).append(n.date().isoformat())
            core.save_state(state)

        while time.monotonic() < deadline and not core._stop:
            core.sleep_jitter(
                extra=extra,
                in_stock_repeat=core.any_in_stock_repeat(state, products, results),
            )
            if time.monotonic() >= deadline or core._stop:
                break
            cfg = maybe_reload_config(cfg, state)
            products = cfg["products"]
            core.flush_pending(cfg, state, products)
            results, extra = run_once(cfg, state, products)
        log.info("Run window finished; next scheduled run continues.")
        return 0
    finally:
        try:
            core.flush_pending(cfg, state, cfg.get("products") or products)
            core.save_state(state)
        except Exception:
            log.exception("Final flush/save failed")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.error("Fatal:\n%s", traceback.format_exc())
        sys.exit(1)
