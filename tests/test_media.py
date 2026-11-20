"""Media / premiere watches: reminders, official-date detection, buttons. No network."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import restockr_core as core
import restockr_media as media

ZELDA = {
    "id": "zelda-movie",
    "name": "The Legend of Zelda",
    "short_name": "Zelda",
    "url": "https://www.nintendo.com/us/movies/the-legend-of-zelda/",
    "emoji": "🗡️",
    "type": "media",
    "format": "film",
    "image": "assets/media/zelda.png",
    "description": "A live-action adventure through Hyrule.",
    "watch_dates": True,
    "releases": [{"id": "us-theatrical", "label": "US theaters", "date": "2027-04-30"}],
}

ONE_PIECE = {
    "id": "the-one-piece",
    "name": "THE ONE PIECE",
    "url": "https://www.netflix.com/tudum/articles/the-one-piece-release-date",
    "emoji": "🏴‍☠️",
    "type": "media",
    "format": "series",
    "watch_dates": True,
    "expected_months": ["2027-02"],
    "us_streaming": "February 2027 on Netflix (exact day TBA)",
}


def parsed(*entries):
    return core.parse_config({"products": list(entries), "topic": "restockr-no-topic"}, require_topic=False)


class TestReminderMath:
    def test_ten_days_before_at_nine_pyt(self):
        when = media.reminder_at_for(date(2027, 4, 30))
        assert when.date() == date(2027, 4, 20)
        assert when.hour == 9 and when.minute == 0
        assert str(when.tzinfo) == "America/Asuncion"

    def test_known_titles(self):
        assert media.reminder_at_for(date(2026, 12, 25)).date() == date(2026, 12, 15)
        assert media.reminder_at_for(date(2027, 2, 12)).date() == date(2027, 2, 2)
        assert media.reminder_at_for(date(2027, 4, 2)).date() == date(2027, 3, 23)
        assert media.reminder_at_for(date(2027, 6, 30)).date() == date(2027, 6, 20)


class TestDueReminders:
    def test_not_before_nine_on_reminder_day(self):
        entry = parsed(ZELDA)["products"][0]
        early = datetime(2027, 4, 20, 8, 59, tzinfo=media.DISPLAY_TZ)
        assert media.due_reminders(entry, {}, now=early) == []
        on_time = datetime(2027, 4, 20, 9, 0, tzinfo=media.DISPLAY_TZ)
        due = media.due_reminders(entry, {}, now=on_time)
        assert len(due) == 1
        assert due[0]["reminder_key"] == "us-theatrical:2027-04-30"

    def test_sent_once(self):
        entry = parsed(ZELDA)["products"][0]
        pstate = {"media_reminders_sent": ["us-theatrical:2027-04-30"]}
        later = datetime(2027, 4, 20, 10, 0, tzinfo=media.DISPLAY_TZ)
        assert media.due_reminders(entry, pstate, now=later) == []

    def test_narnia_two_independent_reminders(self):
        raw = {
            "id": "narnia",
            "name": "Narnia",
            "url": "https://example.com/narnia",
            "type": "media",
            "releases": [
                {"id": "us-theatrical", "label": "US theaters", "date": "2027-02-12"},
                {"id": "us-streaming", "label": "US streaming", "date": "2027-04-02", "platform": "Netflix"},
            ],
        }
        entry = parsed(raw)["products"][0]
        first = media.due_reminders(entry, {}, now=datetime(2027, 2, 2, 9, 0, tzinfo=media.DISPLAY_TZ))
        assert [r["id"] for r in first] == ["us-theatrical"]
        pstate = {}
        media.mark_reminder_sent(pstate, first[0]["reminder_key"])
        mid = media.due_reminders(entry, pstate, now=datetime(2027, 3, 1, 9, 0, tzinfo=media.DISPLAY_TZ))
        assert mid == []
        second = media.due_reminders(entry, pstate, now=datetime(2027, 3, 23, 9, 0, tzinfo=media.DISPLAY_TZ))
        assert [r["id"] for r in second] == ["us-streaming"]


class TestMaybeReminders:
    def test_queues_card_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(core, "TEST_MODE", False)
        monkeypatch.setattr(core, "is_muted", lambda *a, **k: False)
        queued = []

        def fake_emit(*_a, **kw):
            queued.append(kw)
            return {"id": "e"}

        monkeypatch.setattr(core, "emit_alert", fake_emit)
        entry = parsed(ZELDA)["products"][0]
        state = core.default_state()
        cfg = {"products": [entry], "topic": "restockr-no-topic", "_ntfy_enabled": False}
        now = datetime(2027, 4, 20, 9, 0, tzinfo=media.DISPLAY_TZ)
        assert core.maybe_media_reminders(cfg, state, entry, now=now) == 1
        assert queued and queued[0]["kind"] == "media_reminder"
        assert "10 days until US theaters" in queued[0]["event_line"]
        assert core.maybe_media_reminders(cfg, state, entry, now=now) == 0
        assert len(queued) == 1


class TestDateParsing:
    def test_extracts_common_formats(self):
        text = "In theaters April 30, 2027. Also 2 April 2027 and 2027-06-30."
        days = {d["date"] for d in media.extract_full_dates(text)}
        assert date(2027, 4, 30) in days
        assert date(2027, 4, 2) in days
        assert date(2027, 6, 30) in days

    def test_first_check_is_baseline(self):
        html = "The series premieres on Netflix on February 14, 2027."
        ev = media.evaluate_media("THE ONE PIECE", html, html, ONE_PIECE, pstate=None)
        assert ev["triggered"] is False
        assert ev["page_dates"] == ["2027-02-14"]

    def test_new_exact_day_for_expected_month(self):
        html = "All seven episodes arrive on Netflix on February 14, 2027."
        pstate = {"media_dates_baselined": True, "media_page_dates": []}
        ev = media.evaluate_media("THE ONE PIECE", html, html, ONE_PIECE, pstate=pstate)
        assert ev["triggered"] is True
        assert ev["new_dates"][0]["date"] == "2027-02-14"
        assert ev["new_dates"][0]["platform"] == "Netflix"

    def test_ignores_already_known_theatrical_date(self):
        html = "The Legend of Zelda opens in theaters April 30, 2027 worldwide."
        pstate = {"media_dates_baselined": True, "media_page_dates": ["2027-04-30"]}
        ev = media.evaluate_media("Zelda", html, html, ZELDA, pstate=pstate)
        assert ev["triggered"] is False

    def test_new_streaming_date_with_keyword(self):
        html = (
            "In theaters April 30, 2027. "
            "The film begins streaming on Netflix on August 12, 2027."
        )
        pstate = {"media_dates_baselined": True, "media_page_dates": ["2027-04-30"]}
        ev = media.evaluate_media("Zelda", html, html, ZELDA, pstate=pstate)
        assert ev["triggered"] is True
        assert ev["new_dates"][0]["date"] == "2027-08-12"
        assert "streaming" in ev["new_dates"][0]["label"].lower()

    def test_ignores_copyright_and_past_dates(self):
        html = "© November 19, 2018 Nintendo. Updated January 1, 2020."
        pstate = {"media_dates_baselined": True, "media_page_dates": []}
        ev = media.evaluate_media("Zelda", html, html, ZELDA, pstate=pstate)
        assert ev["triggered"] is False

    def test_ambiguous_flood_is_ignored(self):
        html = (
            "Coming May 1, 2027 and June 2, 2027 and July 3, 2027 "
            "in theaters, streaming, and on Netflix."
        )
        pstate = {"media_dates_baselined": True, "media_page_dates": []}
        ev = media.evaluate_media("Zelda", html, html, ZELDA, pstate=pstate)
        assert ev["triggered"] is False


class TestHandleMedia:
    def test_first_check_does_not_alert_then_new_date_does(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(core, "TEST_MODE", False)
        monkeypatch.setattr(core, "is_muted", lambda *a, **k: False)
        queued = []
        monkeypatch.setattr(core, "emit_alert", lambda *a, **kw: queued.append(kw) or {"id": "e"})
        entry = parsed(ONE_PIECE)["products"][0]
        state = core.default_state()
        cfg = {"products": [entry], "topic": "restockr-no-topic", "_ntfy_enabled": False}
        html = "THE ONE PIECE premieres on Netflix on February 14, 2027."
        ev = media.evaluate_media("THE ONE PIECE", html, html, entry, pstate=None)
        core.handle_media(cfg, state, entry, ev)
        assert queued == []
        ev2 = media.evaluate_media(
            "THE ONE PIECE", html, html, entry,
            pstate=core.product_state(state, entry),
        )
        # evaluate sees it as new; handle_media also recomputes against baseline
        # After baseline, the same date should NOT fire (it was already on the page).
        core.handle_media(cfg, state, entry, ev2)
        assert queued == []

        later = "All episodes drop on Netflix on February 14, 2027. Coming June 8, 2028 on Netflix."
        ev3 = media.evaluate_media("THE ONE PIECE", later, later, entry, pstate=core.product_state(state, entry))
        core.handle_media(cfg, state, entry, ev3)
        # June 8, 2028 with Netflix keyword should be a new date
        kinds = [q["kind"] for q in queued]
        assert "media_date" in kinds


def _pyt_ts(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=media.DISPLAY_TZ).timestamp()


class TestShouldCheck:
    def test_daily_cap_and_no_fetch_when_not_watching(self):
        entry = parsed(ZELDA)["products"][0]
        # No watch_from in this fixture, so checks may start immediately.
        state = core.default_state()
        start = _pyt_ts(2026, 11, 19)
        assert core.should_check(state, entry, now=start) is True
        core.product_state(state, entry)["last_media_check"] = start
        assert core.should_check(state, entry, now=start + 86399) is False
        assert core.should_check(state, entry, now=start + 86400) is True
        narnia = parsed({
            "id": "narnia", "name": "Narnia", "url": "https://example.com/n",
            "type": "media", "watch_dates": False,
            "releases": [{"id": "t", "label": "US theaters", "date": "2027-02-12"}],
        })["products"][0]
        assert core.should_check(core.default_state(), narnia, now=start) is False

    def test_cooldown_clamped_to_daily(self):
        entry = parsed({**ZELDA, "cooldown": 60})["products"][0]
        assert entry["cooldown"] == 86400

    def test_watch_from_delays_page_checks_not_reminders(self):
        one = parsed({**ONE_PIECE, "watch_from": "2027-01-01"})["products"][0]
        zelda = parsed({**ZELDA, "watch_from": "2027-03-30"})["products"][0]
        state = core.default_state()
        assert one["watch_from"] == "2027-01-01"
        assert zelda["watch_from"] == "2027-03-30"
        assert core.should_check(state, one, now=_pyt_ts(2026, 12, 31, 23, 59)) is False
        assert core.should_check(state, one, now=_pyt_ts(2027, 1, 1, 0, 0)) is True
        assert core.should_check(state, zelda, now=_pyt_ts(2027, 3, 29, 23, 59)) is False
        assert core.should_check(state, zelda, now=_pyt_ts(2027, 3, 30, 0, 0)) is True

        # A reminder due before watch_from still fires (page checks stay off).
        early = {
            "id": "early-reminder",
            "name": "Early",
            "url": "https://example.com/e",
            "type": "media",
            "watch_from": "2027-04-01",
            "releases": [{"id": "us-theatrical", "label": "US theaters", "date": "2027-03-15"}],
        }
        entry = parsed(early)["products"][0]
        assert core.should_check(core.default_state(), entry, now=_pyt_ts(2027, 3, 5, 9, 0)) is False
        due = media.due_reminders(entry, {}, now=datetime(2027, 3, 5, 9, 0, tzinfo=media.DISPLAY_TZ))
        assert [r["id"] for r in due] == ["us-theatrical"]


class TestButtonsAndCaption:
    def test_media_buttons_say_got_it(self):
        entry = parsed(ZELDA)["products"][0]
        labels = [b["text"] for row in core.build_alert_keyboard(entry)["inline_keyboard"][1:] for b in row]
        assert labels == ["✅ Got it", "🚫 Not interested", "🔔 Keep alerting"]
        muted = core.build_alert_keyboard(entry, "bought")
        text = json.dumps(muted, ensure_ascii=False)
        assert "Got it" in text and "alerts off" in text
        assert "Bought" not in text

    def test_restock_buttons_unchanged(self):
        prod = {"id": "chromatic", "name": "Chromatic", "url": "https://example.com", "type": "restock"}
        labels = [b["text"] for row in core.build_alert_keyboard(prod)["inline_keyboard"][1:] for b in row]
        assert labels == ["✅ Bought it", "🚫 Not buying", "🔔 Keep alerting"]

    def test_caption_has_confirmed_us_lines(self):
        entry = parsed(ZELDA)["products"][0]
        cap = core.build_caption({"status": "watching", "description": entry["description"]}, {}, product=entry)
        assert "US theaters" in cap
        assert "April 30, 2027" in cap
        assert "US streaming" in cap
        assert "TBA" in cap
        assert "Paraguay" not in cap
        assert "Expected on Netflix" not in cap

    def test_live_config_parses_and_images_exist(self):
        raw = json.loads((ROOT / "config.json").read_text())
        cfg = core.parse_config(raw, require_topic=False)
        media_ids = [p["id"] for p in cfg["products"] if p["type"] == "media"]
        assert media_ids == [
            "zelda-movie",
            "chainsaw-man-assassins",
            "the-one-piece",
            "one-piece-film-god-valley",
            "shrek-5",
            "narnia",
            "harry-potter",
        ]
        for p in cfg["products"]:
            if p["type"] != "media":
                continue
            img = ROOT / (p.get("image") or "")
            assert img.is_file(), p["id"]
            assert p["cooldown"] >= 86400
        by_id = {p["id"]: p for p in cfg["products"]}
        assert by_id["the-one-piece"]["watch_from"] == "2027-01-01"
        assert by_id["zelda-movie"]["watch_from"] == "2027-03-30"
        assert by_id["chainsaw-man-assassins"]["watch_from"] is None
        assert by_id["narnia"]["watch_dates"] is False
        assert by_id["harry-potter"]["watch_dates"] is False
