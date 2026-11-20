# Restockr

**Created by Jesús Molina ([@Moljesdev99](https://github.com/Moljesdev99))**

A **free, general-purpose web watcher**. It runs on [GitHub Actions](https://docs.github.com/en/actions), checks the pages you care about every 1 to 2 minutes (every 60–75 seconds on a watch's reminder day), and messages you on [Telegram](https://telegram.org/) when something changes — a product comes back in stock, a price drops, tickets go on sale, a release date appears, a hotel is no longer sold out, or any other text on a page appears, disappears, or changes. You can also receive the same alerts on [ntfy](https://ntfy.sh/).

It is not limited to shop restocks. Restocks are one watch type among several.

## What it does

- Watches one or more web pages, day and night, even if your computer is off.
- Sends a **Telegram card** with a photo (when one is available), a short status, and a button: **🛒 View product** for restocks, **🔗 Open page** for everything else.
- In-stock and waiting-room alerts add **✅ Bought it**, **🚫 Not buying**, and **🔔 Keep alerting** buttons so you can stop or extend the repeats. See [Alert buttons](#alert-buttons).
- Optionally sends the same alert to ntfy.
- Every day at **09:00 Mexico City time** (`America/Mexico_City`), it sends a heartbeat listing every active watch (name, status, expected date, and price), as long as the primary restock item is not in stock. Each product has a Telegram button to open its page. Times in that message are shown in Paraguay time (`America/Asuncion`, labeled PYT).
- It keeps running until you **disable the workflow**. There is no built-in end date.
- A long run reloads `config.json` from this repository about every five minutes, so new or edited watches apply without waiting for the next job.

The **restock** type is tested with **OpenAI Supply Co.** and the **Disney Store** (including Disney’s Queue-it waiting room). The other types (`text_appears`, `text_disappears`, `price_below`, `changed`) work when the information is present in the HTML that the watcher downloads. See [Limitations](#limitations).

## How it works

```
GitHub Actions
  (a ~5 h 40 min run; the next one is queued when it finishes,
   and a 20-minute cron is the backup starter)
        │
        ▼
  The program fetches each watched page
  (an HTTPS request that impersonates Chrome)
  and checks again every 60–120 seconds
  (60–75 seconds on a reminder day, Paraguay time)
        │
        ├─ Stock, price, text, or content matches your rule
        │  (or a virtual waiting room opens, or the page cannot be read)
        │         ▼
        │    Telegram  (+ ntfy, if you configured it)
        │
        └─ Once a day, at 09:00 Mexico City time:
              “🟢 Restockr is running”
              (with each watch, its status, and a button to open the page)
```

It does not use AI and does not spend tokens. Bot credentials are stored as **GitHub Actions secrets**, not in the code.

The first successful check of a generic watch only records a baseline and does **not** send an alert. You are notified on the next change (text appears or disappears, the price falls to or below your threshold, or the content hash changes). Set an optional `cooldown` (seconds) if you also want reminder alerts while the condition stays true.

## Watch types

Existing restock entries keep working if you omit `type` (it defaults to `restock`). You can put everything in `products`, or add extra entries under `watches` — both lists are merged.

| `type` | Triggers when | Useful for |
|---|---|---|
| `restock` | The page looks in stock, the expected date changes, it sells out again, or (Disney) a `queue.*` waiting room opens | Products |
| `text_appears` | A string or regex is found in the page (or only inside `selector` if you set one) | “Buy tickets”, a release date, “Available” |
| `text_disappears` | A string or regex is no longer found in that same scope | “Sold out” disappearing on a hotel or event page |
| `price_below` | An extracted price is at or below `threshold` | Flights, hotels, products — **if the price is in the HTML** |
| `changed` | The text of the page (or of a `selector`) changes compared with the last check | Release-date pages, any “tell me if this changes” use |
| `media` | An official US theatrical or streaming date is 10 days away, or a new official calendar date appears on the title’s official page | Movies, anime, and series premieres |

Top-level option: `"daily_notices": false` stops a second copy of the watcher (for example a backup on another machine that shares the same bot) from sending the restock-day reminder and the daily heartbeat. Stock alerts are still sent.

Common fields on every entry: `id`, `name`, `url`, `type`, `emoji`, optional `image` / `image_url` (a URL or a path in this repo such as `assets/media/poster.jpg`), optional `cooldown` (seconds), optional `is_test` (prefix alerts with 🧪 Test), optional `primary` (daily heartbeat; only one should be true), optional `button_text`.

**`media` watches** track official US release dates. Confirmed dates go in `releases` (`date` as `YYYY-MM-DD`, plus a `label` such as `US theaters` or `US streaming`, and an optional `platform`). Ten days before each of those dates, at 09:00 Paraguay time (`America/Asuncion`, labeled PYT), Restockr sends an image card reminder. If `watch_dates` is true (the default), it also reads the official page about once an hour (cooldown is never shorter than 3600 seconds) and alerts when a new official calendar date appears, then schedules the same 10-day reminder for that date. The first page check only records a baseline and does not alert. Optional `expected_months` (for example `"2027-02"`) help it treat a more specific day in that month as official. Optional `us_theatrical` / `us_streaming` strings are shown on the card when a full date is not known yet (for example `February 2027 on Netflix (exact day TBA)`). Set `format` to `series` to skip the theaters line. Cards use **✅ Got it** / **🚫 Not interested** / **🔔 Keep alerting** (not the restock Bought it labels).

Optional reminder (only if you set it; nothing is hard-coded to a product or date):

- `remind_on`: `"2026-11-19"` (fires at 07:00 Paraguay time, `America/Asuncion`, labeled PYT) or `"2026-11-19 07:00"`
- optional `timezone` on the same watch (an IANA name such as `America/Mexico_City`) to interpret a naive `remind_on` time in that zone instead of Paraguay time
- or `expected_date`: `"2026-11-19"` together with `"reminder": true`
- an explicit UTC offset in `remind_on` (for example `"2026-11-19 07:00-03:00"`) is honored as-is

Other parameters can go in a `params` object or directly on the entry: `text`, `pattern`, `selector`, `threshold`, `currency`, `json_path`, `ignore`, `inclusive`.

**CSS `selector`:** when you set one, the watcher searches *only* inside that section. If the selector matches nothing, that check is an error (after several failures you get a ⚠️ warning). It is not treated as “the text disappeared” or as a valid page change.

**Prices:** a locator you provide (`selector`, `pattern`, or `json_path`) is used instead of JSON-LD or other prices on the page (those often belong to related items). Supported number formats include `19.99`, `19,99`, `1,299.00`, and `1.299,00`. A lone `1,299` or `1.299` is ambiguous and will **not** raise an alert. If several *different* prices are found and you did not give a locator, the watcher also stays silent and logs the ambiguity.

**Cooldown:** if the page changes again while a cooldown is running, the latest value is kept and sent when the cooldown ends (it is not dropped).

**Alerts:** a message is only marked delivered on a channel (Telegram or ntfy) after that channel succeeds. Failures are retried with backoff and stored in state so the next loop or job can try again. A channel that already succeeded is not resent.

## Use-case examples

Copy an example into your `config.json`, then put **your** URL and wording. The URLs below are placeholders except where noted.

### Product restock

```json
{
  "id": "chromatic",
  "name": "Game Boy Chromatic Codex",
  "short_name": "the Chromatic Codex",
  "url": "https://openai.com/supply/co-lab/chromatic/",
  "emoji": "🎮",
  "type": "restock",
  "primary": true,
  "remind_on": "2026-11-19 06:00",
  "timezone": "America/Mexico_City"
}
```

`short_name` is a short form of the product name. `remind_on` is optional; omit it unless you want a same-day reminder for *that* watch. The example above fires at 06:00 Mexico City time (09:00 PYT).

### Movie or series release / tickets on sale

Watch an official title page, a theater page, or a “tickets on sale” page that already shows the words you care about in the HTML:

```json
{
  "id": "movie-tickets",
  "name": "Movie tickets",
  "url": "https://www.example.com/movie",
  "emoji": "🎟️",
  "type": "text_appears",
  "text": "Buy tickets",
  "cooldown": 3600
}
```

For a release date that may be filled in later, watch just that section for any change:

```json
{
  "id": "series-release",
  "name": "Series release date",
  "url": "https://www.example.com/series",
  "emoji": "📺",
  "type": "changed",
  "selector": "#release-date",
  "cooldown": 1800
}
```

Streaming catalogs (Netflix, Disney+, and similar) often build the page with JavaScript or block automated requests. Prefer the studio’s official page or a public API/HTML listing when you can.

For a movie or series with official US dates (and/or a page to watch until a date is announced), use `type: media` instead of a raw `changed` watch. Example:

```json
{
  "id": "example-movie",
  "name": "Example movie",
  "url": "https://www.example.com/movie",
  "emoji": "🎬",
  "type": "media",
  "format": "film",
  "image": "assets/media/poster.jpg",
  "description": "A short official description.",
  "button_text": "🔗 Official page",
  "watch_dates": true,
  "releases": [
    {"id": "us-theatrical", "label": "US theaters", "date": "2027-06-30"}
  ],
  "us_streaming": "TBA"
}
```

### Flight price drop

```json
{
  "id": "flight-mex-asuncion",
  "name": "Flight price",
  "url": "https://www.example.com/your-fare-or-alert-page",
  "emoji": "✈️",
  "type": "price_below",
  "threshold": 400,
  "currency": "USD",
  "selector": ".price",
  "cooldown": 3600
}
```

**Not tested on Google Flights or airline booking engines.** Those sites are JavaScript-heavy and usually bot-protected, so this watcher often cannot see the fare. It *was* tested on a public HTML catalog with a visible price (`books.toscrape.com`). Use a fare page, itinerary, or alert link that shows the amount in the HTML, a JSON-LD `Offer`, or a documented API.

### Hotel availability or price

```json
{
  "id": "hotel-weekend",
  "name": "Hotel weekend",
  "url": "https://www.example.com/hotel-dates",
  "emoji": "🏨",
  "type": "text_disappears",
  "text": "Sold out",
  "cooldown": 3600
}
```

Or use `price_below` if the nightly rate is in the HTML. Large hotel sites (Booking, Airbnb, and similar) often hide availability behind JavaScript or anti-bot checks; results there are not guaranteed.

### Event tickets on sale

```json
{
  "id": "concert-onsale",
  "name": "Concert on sale",
  "url": "https://www.example.com/event",
  "emoji": "🎫",
  "type": "text_appears",
  "text": "Buy tickets",
  "pattern": "Buy tickets|On sale now",
  "cooldown": 1800
}
```

Ticketmaster and similar platforms may use waiting rooms or bot protection. If the request is redirected to a `queue.*` host on a restock watch, you get a waiting-room alert; on other types the check is treated as a failure so a queue page is not mistaken for a change.

### Generic page change

```json
{
  "id": "any-page",
  "name": "Page change",
  "url": "https://www.example.com/status",
  "emoji": "🔔",
  "type": "changed",
  "selector": "main",
  "ignore": ["Last updated .*"],
  "cooldown": 1800
}
```

`ignore` is a regex (or list of regexes) stripped before the hash, so a ticking “last updated” clock does not spam you.

A full starter file is in [`config.example.json`](config.example.json).

## Alerts you may receive

| Alert | Meaning |
|---|---|
| ☁️ Restockr cloud watcher: active | Startup or test run: whether each page could be read |
| 📦 Status: 🟢 In stock, buy now! | Restock: the product is available. Repeated every 2 minutes, up to 30 times (about 1 hour), or with no limit after **🔔 Keep alerting**. Carries the alert buttons below |
| 📅 Restock date changed | The page now shows a different expected date |
| ⚪ Sold out again | It was in stock and sold out again |
| 🚦 The virtual waiting room is open! Join now | Restock: the page redirected to a `queue.*` host or a Queue-it page (as Disney does). Up to 3 alerts, 3 minutes apart |
| ✅ The waiting room is over, the product page is open again | Restock: after a waiting room, the real product page loads again (2 reads in a row, or 1 if it is already in stock). Sent once, with the current Status; an in-stock alert follows if it can be bought |
| 🟢 “Buy tickets” is now on the page | `text_appears` |
| 🟢 “Sold out” is no longer on the page | `text_disappears` |
| 🟢 Price is US$180.00 (threshold US$200.00) | `price_below` |
| 🔔 The page content changed | `changed` |
| ⏰ Restock is today / Today is the expected date | Only if that watch has `remind_on` (or `expected_date` + `reminder`) and that date has been reached |
| ⏰ N days until US theaters / US streaming (date) | `media`: 10 days before a confirmed US date, at 09:00 PYT, as an image card |
| 📅 Official date announced | `media`: a new official calendar date appeared on the official page |
| 🟢 Restockr is running | Daily heartbeat listing every active watch, with a button to open each page (if the primary restock item is not in stock) |
| ⚠️ I could not read … N times in a row | 5 consecutive failures (possible block). It keeps retrying, with longer waits |
| ✅ Watcher recovered | It could read the page again after that block |

If an entry has `"is_test": true`, its alerts are prefixed with **🧪 Test**.

### Alert buttons

In-stock, waiting-room-open, and waiting-room-over cards for a `restock` watch carry, under **🛒 View product**, three buttons:

| Button | Effect |
|---|---|
| **✅ Bought it** | Stops every alert for that watch (in stock, waiting room, date change, sold out, reminder). The card changes to “✅ Bought — alerts off” with a **↩️ Resume** button |
| **🚫 Not buying** | Same as above, recorded as skipped |
| **🔔 Keep alerting** | Removes the 30-repeat limit: you are alerted every 2 minutes for as long as it is in stock, including after it sells out and restocks, until you tap Bought it or Not buying |

If you do not tap anything, alerts keep repeating every 2 minutes, up to 30 times per restock.

A muted watch stays muted (also across restocks) until you tap **↩️ Resume** or send `/reset <id>` (or `/reset all`). A muted restock watch is then only read every 30 minutes, so the daily heartbeat stays accurate without using a check every minute. Other watches are not affected.

Reminder and “official date announced” cards for a `media` watch carry the same three actions with movie/series wording:

| Button | Effect |
|---|---|
| **✅ Got it** | Stops further alerts for that title. The card changes to “✅ Got it — alerts off” with a **↩️ Resume** button |
| **🚫 Not interested** | Same as above, recorded as skipped |
| **🔔 Keep alerting** | Keep sending the 10-day reminders and official-date alerts for that title |

Commands you can send to the bot: `/status` (each watch, its status and alert mode) and `/reset <id|all>` (alias `/unmute`). Only messages from your `TG_CHAT` are accepted.

**How taps are read.** There is no webhook server. While waiting between checks, each watcher calls Telegram’s `getUpdates` every 3 seconds (callback queries and messages only) and answers the tap with a short confirmation. Because the cloud watcher and an optional second watcher can share the same bot, updates are **not** consumed right away: each watcher stores the last `update_id` it handled in its own `state.json`, and an update is only confirmed (removed from Telegram’s queue) 12 hours after it was first seen. So both watchers apply every tap to their own state. Only the cloud watcher replies to `/status` and `/reset`. Do not point a webhook or another `getUpdates` consumer at the same bot.

## Requirements

- A [GitHub](https://github.com/signup) account. No credit card is required. The repository can be **public** (Actions minutes are generally unlimited on the free plan) or **private** (monthly minute quota).
- A [Telegram](https://telegram.org/) account.
- About 15 minutes for the initial setup.

## Setup (step by step)

### 1. Create your own copy of the project

1. Open [https://github.com/Moljesdev99/restockr](https://github.com/Moljesdev99/restockr) (sign in to GitHub).
2. Click the green **Use this template** button → **Create a new repository**.
   - If you do not see that button, click **Fork**.
3. Choose a name (for example `restockr`), leave it **Public** if you want the free Actions minutes, and create the repository.

The code then lives under *your* account. Alerts go to you, not to the author.

### 2. Create a Telegram bot

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a display name (for example `Restockr`) and a username that ends in `bot`.
3. BotFather gives you a **token**. Copy it and keep it private. **Do not share it or commit it to the repository.**
4. Open your new bot (the link BotFather gave you) and tap **Start** or send `/start`. Without this step, the bot cannot message you.

### 3. Find your chat id

1. Send any message to your bot (`/start` is enough).
2. In a browser, open this URL, replacing `YOUR_TOKEN` with the token from the previous step:

   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`

3. In the text that appears, look for something like `"chat":{"id": 123456789`. That number is your **chat id**.
   - In a private chat it is usually a positive number.
   - In a group it is usually negative. If you use a group, invite the bot and send a message there before opening the URL.

If the page shows `"ok":true,"result":[]`, send the bot another message and reload.

### 4. (Optional) Set up ntfy

ntfy is a backup channel if Telegram fails.

1. Invent a **long, hard-to-guess** topic name, like a password. Anyone who knows that name can read your alerts on ntfy.sh.
2. Subscribe to that topic in the [ntfy app](https://ntfy.sh/) or at `https://ntfy.sh/YOUR_TOPIC`.
3. You will store it later as the `NTFY_TOPIC` secret. If you skip this, the watcher uses Telegram only.

### 5. Store the keys as GitHub secrets

In *your* repository:

1. Go to **Settings** → **Secrets and variables** → **Actions**.
2. Click **New repository secret** and create these two (the names must match exactly):

   | Name | Value |
   |---|---|
   | `TG_TOKEN` | The token BotFather gave you |
   | `TG_CHAT` | Your chat id (the number only) |

3. If you use ntfy, also create `NTFY_TOPIC` with your topic name.

Never put these values in `config.json`, in the README, or in a commit message.

### 6. Put your watches in `config.json`

In your repository, open `config.json` → the pencil icon. Start from [`config.example.json`](config.example.json) or from one of the use-case snippets above. Then click **Commit changes**.

This repository’s live `config.json` includes the restock products the project was originally tested with: the Game Boy Chromatic Codex (`chromatic`) and the ModRetro Chromatic Case Codex Edition (`chromatic-case`), both on OpenAI Supply Co., and the Disney Ezra Legacy Lightsaber Set (`disney-ezra`). Each has its own alert buttons and `/reset <id>`. Replace them with yours when you use your own copy.

### 7. Enable GitHub Actions and run a test

1. In your repository, open the **Actions** tab.
2. If GitHub asks you to confirm that you trust the workflows, accept.
3. In the left-hand list, open **Restockr**.
4. Click **Run workflow**, choose `test`, and confirm.

**Test mode** checks every page and sends **one** summary. It does not send regular stock/price/change alerts and does not read or write the live watcher’s `state.json` (it uses a throwaway file).

Within a minute or two you should get **☁️ Restockr cloud watcher: active** on Telegram, with each item’s status. If that arrives, run the workflow again with `watch` so it keeps checking. The scheduled cron will also start it on its own.

To evaluate pages locally without sending alerts:

```bash
pip install -r requirements.txt
MODE=dry python restockr.py
```

Unit tests (simulated HTML plus a few public pages) live in `tests/test_watches.py`.

## Adding or removing watches

Edit `config.json`, add or delete entries in `products` or `watches`, and **Commit**. The change applies on the next run.

You do not need to edit `state.seed.json`. That file is only an initial state the first time (or if GitHub drops memory between runs). New watches start with a blank state.

## Customizing frequency and dates

These values live in the code, not in `config.json`. Change them in a commit if you need something different:

| What | Where | Current value |
|---|---|---|
| Pause between checks | `CHECK_MIN_S` and `CHECK_MAX_S` in `restockr_core.py` | 60 to 120 seconds (tightens to 60–75 seconds on a watch's reminder day in Paraguay time) |
| How often GitHub starts a run | `cron` in `.github/workflows/restockr.yml`, plus a dispatch at the end of each successful or cancelled watch | minutes 7, 27, and 47 of every hour, with the next job queued as soon as the current one finishes saving state |
| Length of each run | `RUN_MINUTES` in the workflow | 340 minutes (~5 h 40 min) |
| Daily heartbeat | `HEARTBEAT_HOUR` and `HEARTBEAT_MINUTE` in `restockr_core.py` | 09:00, `America/Mexico_City` (12:00 PYT) |
| Same-day reminder | `remind_on` or `expected_date` + `reminder` on the watch | Optional; naive times (and date-only values, which fire at 07:00) are in Paraguay time (`America/Asuncion`) unless you set `timezone` or an explicit offset |
| How often config is reloaded | `CONFIG_RELOAD_S` in `restockr_core.py` | 300 seconds (from the repo, when `GITHUB_TOKEN` is available) |

## How runs and state work

The GitHub cron does **not** check pages by itself: it only starts a job. Inside the job, the program checks every 1 to 2 minutes (every 60–75 seconds on a watch's reminder day, in Paraguay time) for about 5 h 40 min (`RUN_MINUTES` is 340; the job timeout is 355 minutes, under GitHub’s 6-hour cap). State is written to `state.json` after each check, including undelivered alerts. On success, cancel, or timeout, the workflow uploads that file (Actions cache, plus an artifact as backup) **after** the watch step. A successful or cancelled watch then queues the next run immediately. The concurrency group (`restockr-watch`) allows only one active watch job; the next job waits until the current one — including the save step — has finished, so a new run should not start until state has been stored.

GitHub keeps only **one pending run** per concurrency group and cancels older queued runs. If a job is still running when several cron ticks fire, you do not get a pile of backups — only one wait in line. The end-of-job dispatch is meant to keep the handoff short (checkout and install of the next job, typically a few minutes). Cron remains as a backup: if a job fails early and does not dispatch the next one, or if a pending run is dropped, there can still be a gap until the next cron (up to about 20 minutes). The process also handles `SIGTERM` by flushing pending alerts and saving state.

Public scheduled workflows can be **disabled after 60 days of inactivity** (no pushes or other repo activity). Visit the Actions tab or push a commit to wake them.

To stop: disable the workflow. There is no automatic end date.

## How to turn it off

In your repository: **Actions** → **Restockr** → the **…** menu → **Disable workflow**.

## Troubleshooting

**I did not get the test message**
- Confirm the secrets are named exactly `TG_TOKEN` and `TG_CHAT`.
- Confirm you sent `/start` to your bot.
- Open **Actions** → the latest run → the **Watch** step and look for an error (for example “Missing TG_TOKEN / TG_CHAT”).

**The message says it could not read a page**
- The site may be blocking the watcher (Cloudflare, a captcha, or a waiting room).
- The page may depend on heavy JavaScript, so the HTML has no stock, price, or text to read.
- For restocks, try an OpenAI Supply or Disney Store page first; those are the ones already verified.

**The alert arrived late**
- GitHub Actions cron jobs are sometimes delayed by several minutes.
- If a run fails or ends early, there can be a gap until the next one.

**I am getting duplicate alerts**
- This often happens if another copy of the watcher is also running (another computer or another repository).
- If GitHub drops memory between runs (`state.json`), some alerts may fire once more.

**ntfy is not arriving**
- The `NTFY_TOPIC` secret is optional: if it is missing, only Telegram is used.
- Check that the topic name matches exactly and that you are subscribed to that same topic.
- ntfy.sh’s free plan limits how many messages you can receive per day.

**It stopped running**
- Check whether the workflow is disabled on the **Actions** tab.
- On a public repo, a scheduled workflow may have been paused after 60 days of inactivity — push a commit or re-enable it under Actions.

## Limitations

- **GitHub Actions delays and drops.** The `cron` schedule is not second-accurate; jobs can start late or, occasionally, be skipped. Only one pending run is kept per concurrency group.
- **Six-hour job limit.** A single GitHub-hosted job cannot run longer than 6 hours. This project stays under that on purpose and chains via cron + concurrency. A crash before state is saved can lose in-progress delivery retries until the next successful restore.
- **60-day inactivity.** On public repositories, scheduled workflows may be disabled after 60 days without repository activity. A push or a visit to Actions is enough to re-enable them.
- **GitHub terms of use.** Actions is intended mainly for software-development work (tests, publishing code, and similar). Using it as a personal always-on service may fall outside that, and GitHub could limit or stop the workflow. Use it at your own risk.
- **Bot protection and JavaScript.** The watcher downloads HTML with a Chrome-like request. It does not run a full browser session. Sites with Cloudflare, captchas, virtual queues, or pages that only fill in stock/price after JavaScript may fail. It warns after 5 consecutive failures and keeps retrying.
- **What was actually tested.** Restock detection is tuned for OpenAI Supply Co. and the Disney Store (including Queue-it when the final host starts with `queue.`). On Disney the page always contains a hidden “Out of Stock” template, so the watcher reads the buy button and the page’s availability, then confirms with Disney’s `Product-Variation` JSON (`available` / `readyToOrder`). While the waiting room is on, the watcher has no queue pass, so it cannot see stock until Disney turns the waiting room off; join the queue yourself when the 🚦 alert arrives. Generic types were tested with simulated HTML, [example.com](https://example.com/) (`text_appears`, `text_disappears`, `changed`), and a public HTML shop ([books.toscrape.com](https://books.toscrape.com/)) for `price_below`. **Google Flights, airline sites, major hotel/booking sites, and streaming catalogs were not verified** and often will not work. Prefer pages that already show the information in the HTML, official release-date pages, or a documented API.
- **State is not in the git tree.** It lives in the Actions cache (and a short-lived artifact). If both are gone, the watcher starts again from `state.seed.json` or from a blank state. Do not commit `state.json`.

## Privacy

- **Never commit** the bot token, chat id, or ntfy topic to the repository, and do not paste them into issues or the README.
- Store them only as GitHub secrets (**Settings** → **Secrets and variables** → **Actions**).
- If the repository is public, other people can see your `config.json` (names and URLs). They cannot see the secrets.
- The ntfy topic works like a password: choose a long one and do not publish it.
- [`.gitignore`](.gitignore) ignores `state.json`, logs, and typical local credential files (`.tg_token`, `.tg_chat`, `.env`).

## License

[MIT](LICENSE). Copyright © 2026 Jesús Molina.

---

**Created by Jesús Molina ([@Moljesdev99](https://github.com/Moljesdev99))**
