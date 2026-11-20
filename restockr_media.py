#!/usr/bin/env python3
"""Media / premiere watches: official US dates, 10-day reminders, page date watch."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DISPLAY_TZ = ZoneInfo("America/Asuncion")
MEDIA_TYPES = {"media", "release", "premiere"}
MEDIA_REMINDER_DAYS = 10
MEDIA_REMINDER_HOUR = 9
MEDIA_REMINDER_MINUTE = 0
MEDIA_MIN_COOLDOWN_S = 86400  # once a day, not more often
MEDIA_CONTROL_KINDS = ("media_reminder", "media_date")
# Do not treat a flood of new dates (a redesigned page) as official announcements.
MEDIA_MAX_NEW_DATES = 2
MEDIA_MAX_YEARS_AHEAD = 4

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_KEYWORDS = re.compile(
    r"\b(release|releases|released|premiere|premier|premieres|arrives|"
    r"arriving|coming|theaters?|theatres?|theatrical|streaming|stream|"
    r"netflix|hbo|peacock|crunchyroll|disney\+|only on|debut|airs|drops|"
    r"watch at home|coming soon|now streaming|digital|pvod|buy or rent|"
    r"in theaters|in theatres|imax)\b",
    re.I,
)
# Month-name dates: "April 30, 2027" / "30 April 2027"
_RE_MDY = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
    re.I,
)
_RE_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\.?,?\s+(\d{{4}})\b",
    re.I,
)
_RE_ISO = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
_RE_MONTH_YEAR = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{4}})\b",
    re.I,
)


def is_media(entry: dict | None) -> bool:
    raw = str((entry or {}).get("type") or "").strip().lower()
    return raw in MEDIA_TYPES


def now_pyt() -> datetime:
    return datetime.now(DISPLAY_TZ)


def parse_iso_date(raw) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def format_us_date(value: date | None) -> str:
    if not value:
        return "TBA"
    return value.strftime("%B ") + str(value.day) + value.strftime(", %Y")


def reminder_at_for(release_date: date, *, days: int = MEDIA_REMINDER_DAYS) -> datetime:
    """09:00 PYT on the calendar day `days` before the official US release date."""
    day = release_date - timedelta(days=days)
    return datetime(
        day.year, day.month, day.day,
        MEDIA_REMINDER_HOUR, MEDIA_REMINDER_MINUTE,
        tzinfo=DISPLAY_TZ,
    )


def normalize_release(raw: dict, *, source: str = "config") -> dict | None:
    if not isinstance(raw, dict):
        return None
    d = parse_iso_date(raw.get("date") or raw.get("on") or raw.get("release_date"))
    if not d:
        return None
    rid = str(raw.get("id") or raw.get("kind") or d.isoformat())
    label = str(raw.get("label") or raw.get("kind") or "US release").strip() or "US release"
    platform = raw.get("platform") or raw.get("service")
    return {
        "id": rid,
        "date": d.isoformat(),
        "label": label,
        "platform": None if platform in (None, "") else str(platform),
        "source": source,
    }


def config_releases(entry: dict | None) -> list[dict]:
    out = []
    seen = set()
    for raw in (entry or {}).get("releases") or []:
        item = normalize_release(raw, source="config")
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        out.append(item)
    return out


def discovered_releases(pstate: dict | None) -> list[dict]:
    out = []
    for raw in (pstate or {}).get("discovered_releases") or []:
        item = normalize_release(raw, source="discovered")
        if item:
            out.append(item)
    return out


def merged_releases(entry: dict | None, pstate: dict | None = None) -> list[dict]:
    """Config releases first, then discovered dates not already known."""
    known_days = set()
    out = []
    for item in config_releases(entry) + discovered_releases(pstate):
        day = item["date"]
        if day in known_days:
            continue
        known_days.add(day)
        out.append(item)
    out.sort(key=lambda r: r["date"])
    return out


def known_date_set(entry: dict | None, pstate: dict | None = None) -> set[date]:
    dates = set()
    for item in merged_releases(entry, pstate):
        d = parse_iso_date(item.get("date"))
        if d:
            dates.add(d)
    for raw in (pstate or {}).get("media_page_dates") or []:
        d = parse_iso_date(raw)
        if d:
            dates.add(d)
    return dates


def known_month_years(entry: dict | None) -> set[tuple[int, int]]:
    """Month-year hints such as 'February 2027' (no exact day yet)."""
    found: set[tuple[int, int]] = set()
    for raw in (entry or {}).get("expected_months") or []:
        text = str(raw).strip()
        m = re.match(r"^(\d{4})-(\d{1,2})$", text)
        if m:
            found.add((int(m.group(1)), int(m.group(2))))
            continue
        m = _RE_MONTH_YEAR.search(text)
        if m:
            found.add((int(m.group(2)), _MONTHS[m.group(1).lower()]))
    for item in config_releases(entry):
        d = parse_iso_date(item.get("date"))
        if d:
            found.add((d.year, d.month))
    return found


def _valid_ymd(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_full_dates(text: str) -> list[dict]:
    """Extract calendar dates (year+month+day) with a short surrounding snippet."""
    text = text or ""
    found: list[dict] = []
    seen: set[date] = set()

    def add(d: date | None, start: int, end: int) -> None:
        if not d or d in seen:
            return
        seen.add(d)
        left = max(0, start - 80)
        right = min(len(text), end + 80)
        snippet = re.sub(r"\s+", " ", text[left:right]).strip()
        found.append({
            "date": d,
            "text": format_us_date(d),
            "snippet": snippet,
            "has_keyword": bool(_DATE_KEYWORDS.search(snippet)),
        })

    for m in _RE_MDY.finditer(text):
        add(_valid_ymd(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))), m.start(), m.end())
    for m in _RE_DMY.finditer(text):
        add(_valid_ymd(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))), m.start(), m.end())
    for m in _RE_ISO.finditer(text):
        add(_valid_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.start(), m.end())
    return found


def infer_label(snippet: str, entry: dict | None = None) -> str:
    text = (snippet or "").lower()
    if any(w in text for w in ("netflix", "hbo", "peacock", "crunchyroll", "streaming", "stream", "disney+")):
        platform = None
        for name in ("Netflix", "HBO Max", "HBO", "Peacock", "Crunchyroll", "Disney+"):
            if name.lower() in text or (name == "HBO Max" and "hbo max" in text):
                platform = "HBO Max" if "hbo max" in text or "hbo" in text and "max" in text else name
                if name == "HBO" and "hbo max" in text:
                    platform = "HBO Max"
                break
        if "hbo" in text and "max" in text:
            platform = "HBO Max"
        if platform:
            return f"US streaming ({platform})"
        return "US streaming"
    if any(w in text for w in ("theater", "theatre", "theatrical", "imax", "in theaters", "in theatres")):
        return "US theaters"
    # If a theatrical date is already known, a later new date is likely streaming.
    existing = [parse_iso_date(r["date"]) for r in config_releases(entry or {})]
    existing = [d for d in existing if d]
    if existing and any("theater" in (r.get("label") or "").lower() for r in config_releases(entry or {})):
        return "US streaming"
    return "US release"


def infer_platform(snippet: str, label: str) -> str | None:
    text = (snippet or "").lower() + " " + (label or "").lower()
    if "crunchyroll" in text:
        return "Crunchyroll"
    if "peacock" in text:
        return "Peacock"
    if "hbo max" in text or ("hbo" in text and "max" in text):
        return "HBO Max"
    if "hbo" in text:
        return "HBO"
    if "netflix" in text:
        return "Netflix"
    if "disney+" in text or "disney plus" in text:
        return "Disney+"
    return None


def _plausible(d: date, *, today: date, hint_months: set[tuple[int, int]]) -> bool:
    if d < today:
        return False
    if d.year > today.year + MEDIA_MAX_YEARS_AHEAD:
        return False
    return True


def new_official_dates(
    text: str,
    entry: dict | None,
    pstate: dict | None,
    *,
    today: date | None = None,
) -> list[dict]:
    """Return newly appeared official-looking dates, or [] if ambiguous."""
    today = today or now_pyt().date()
    known = known_date_set(entry, pstate)
    hints = known_month_years(entry)
    first = not (pstate or {}).get("media_dates_baselined")
    extracted = extract_full_dates(text)
    newcomers = []
    for item in extracted:
        d = item["date"]
        if d in known:
            continue
        if not _plausible(d, today=today, hint_months=hints):
            continue
        hinted = (d.year, d.month) in hints
        if first:
            continue
        # A more precise day for a month we already expected, or a date next
        # to release wording, counts as official enough to surface.
        if item["has_keyword"] or hinted:
            newcomers.append(item)
    # Unique by date
    uniq: dict[date, dict] = {}
    for item in newcomers:
        uniq.setdefault(item["date"], item)
    newcomers = list(uniq.values())
    if len(newcomers) > MEDIA_MAX_NEW_DATES:
        return []
    return newcomers


def all_extracted_date_strings(text: str) -> list[str]:
    return [item["date"].isoformat() for item in extract_full_dates(text)]


def evaluate_media(title: str, html: str, visible: str, entry: dict, pstate: dict | None = None) -> dict:
    """Inspect an official page for newly announced calendar dates."""
    text = " ".join(p for p in (title or "", visible or "", html or "") if p)
    extracted = extract_full_dates(text)
    fresh = new_official_dates(text, entry, pstate)
    description = (entry or {}).get("description")
    image = (entry or {}).get("image") or (entry or {}).get("image_url")
    triggered = bool(fresh)
    return {
        "status": "date_found" if triggered else "watching",
        "triggered": triggered,
        "product_ok": True,
        "watch_type": "media",
        "description": description,
        "image_url": image,
        "title": title,
        "extracted": [
            {
                "date": item["date"].isoformat(),
                "text": item["text"],
                "snippet": item["snippet"],
                "has_keyword": item["has_keyword"],
            }
            for item in extracted
        ],
        "new_dates": [
            {
                "date": item["date"].isoformat(),
                "text": item["text"],
                "snippet": item["snippet"],
                "label": infer_label(item["snippet"], entry),
                "platform": infer_platform(item["snippet"], infer_label(item["snippet"], entry)),
            }
            for item in fresh
        ],
        "page_dates": [item["date"].isoformat() for item in extracted],
        "snippet": (fresh[0]["snippet"] if fresh else "")[:200],
    }


def media_needs_page_watch(entry: dict | None) -> bool:
    return bool((entry or {}).get("watch_dates", True))


def parse_watch_from(entry: dict | None):
    """Optional first day (America/Asuncion) to start fetching the official page."""
    raw = (entry or {}).get("watch_from") or (entry or {}).get("check_from")
    if raw in (None, ""):
        return None
    d = parse_iso_date(raw)
    if not d:
        raise ValueError(f"Invalid watch_from: {raw!r}")
    return d


def media_asof_date(now=None):
    if now is None:
        return now_pyt().date()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=DISPLAY_TZ)
        return now.astimezone(DISPLAY_TZ).date()
    if isinstance(now, date) and not isinstance(now, datetime):
        return now
    return datetime.fromtimestamp(float(now), DISPLAY_TZ).date()


def media_page_watch_open(entry: dict | None, now=None) -> bool:
    """True when this media watch may fetch its official page."""
    if not media_needs_page_watch(entry):
        return False
    start = parse_watch_from(entry)
    if start is None:
        return True
    return media_asof_date(now) >= start


def media_check_interval(entry: dict | None) -> int:
    raw = (entry or {}).get("cooldown")
    try:
        value = int(raw) if raw not in (None, "") else MEDIA_MIN_COOLDOWN_S
    except (TypeError, ValueError):
        value = MEDIA_MIN_COOLDOWN_S
    return max(value, MEDIA_MIN_COOLDOWN_S)


def days_until(release_date: date, *, today: date | None = None) -> int:
    today = today or now_pyt().date()
    return (release_date - today).days


def reminder_event_line(release: dict, *, today: date | None = None) -> str:
    d = parse_iso_date(release.get("date"))
    label = release.get("label") or "US release"
    left = days_until(d, today=today) if d else None
    when = format_us_date(d)
    if left is None:
        return f"⏰ <b>Reminder: {label}</b>"
    if left <= 0:
        return f"⏰ <b>Today: {label} ({when})</b>"
    if left == 1:
        return f"⏰ <b>Tomorrow: {label} ({when})</b>"
    return f"⏰ <b>{left} days until {label} ({when})</b>"


def date_found_event_line(found: dict) -> str:
    label = found.get("label") or "US release"
    when = found.get("text") or format_us_date(parse_iso_date(found.get("date")))
    platform = found.get("platform")
    extra = f" on {platform}" if platform and platform.lower() not in label.lower() else ""
    return f"📅 <b>Official date announced: {label} — {when}{extra}</b>"


def is_series(entry: dict | None) -> bool:
    return str((entry or {}).get("format") or "").strip().lower() in {"series", "show", "tv"}


def us_lines(releases: list[dict], entry: dict | None = None) -> list[str]:
    """Caption lines for confirmed US theatrical / streaming dates only."""
    entry = entry or {}
    theaters = [r for r in releases if "theater" in (r.get("label") or "").lower()]
    streaming = [r for r in releases if "stream" in (r.get("label") or "").lower() or r.get("platform")]
    other = [r for r in releases if r not in theaters and r not in streaming]
    lines = []

    def fmt(r: dict) -> str:
        bit = format_us_date(parse_iso_date(r.get("date")))
        platform = r.get("platform")
        if platform:
            return f"{bit} on {platform}"
        return bit

    if theaters:
        lines.append("🇺🇸 US theaters: " + "; ".join(fmt(r) for r in theaters))
    elif entry.get("us_theatrical"):
        lines.append(f"🇺🇸 US theaters: {entry['us_theatrical']}")
    elif not is_series(entry):
        lines.append("🇺🇸 US theaters: TBA")

    if streaming:
        lines.append("🏠 US streaming: " + "; ".join(fmt(r) for r in streaming))
    elif entry.get("us_streaming"):
        lines.append(f"🏠 US streaming: {entry['us_streaming']}")
    elif other:
        lines.append("🇺🇸 US: " + "; ".join(f"{fmt(r)} ({r.get('label')})" for r in other))
    else:
        lines.append("🏠 US streaming: TBA")
    return lines


def due_reminders(
    entry: dict,
    pstate: dict | None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Releases whose 10-day (09:00 PYT) reminder is due and not yet sent."""
    now = now or now_pyt()
    if now.tzinfo is None:
        now = now.replace(tzinfo=DISPLAY_TZ)
    now = now.astimezone(DISPLAY_TZ)
    sent = set((pstate or {}).get("media_reminders_sent") or [])
    due = []
    for release in merged_releases(entry, pstate):
        d = parse_iso_date(release.get("date"))
        if not d:
            continue
        when = reminder_at_for(d)
        key = f"{release.get('id')}:{d.isoformat()}"
        if now >= when and key not in sent:
            due.append({**release, "reminder_key": key, "reminder_at": when})
    return due


def mark_reminder_sent(pstate: dict, key: str) -> None:
    sent = list(pstate.get("media_reminders_sent") or [])
    if key not in sent:
        sent.append(key)
    pstate["media_reminders_sent"] = sent


def add_discovered(pstate: dict, found: dict) -> dict | None:
    """Persist a newly announced official date. Returns the release or None if known."""
    d = parse_iso_date(found.get("date"))
    if not d:
        return None
    existing = {item.get("date") for item in (pstate.get("discovered_releases") or [])}
    # Also skip if already listed
    if d.isoformat() in existing:
        return None
    release = normalize_release(
        {
            "id": f"discovered-{d.isoformat()}",
            "date": d.isoformat(),
            "label": found.get("label") or "US release",
            "platform": found.get("platform"),
        },
        source="discovered",
    )
    if not release:
        return None
    bucket = list(pstate.get("discovered_releases") or [])
    bucket.append(release)
    pstate["discovered_releases"] = bucket
    return release


def baseline_page_dates(pstate: dict, page_dates: list[str]) -> None:
    pstate["media_page_dates"] = list(dict.fromkeys(page_dates or []))
    pstate["media_dates_baselined"] = True
