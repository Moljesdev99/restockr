# Restockr

**Created by Jesús Molina ([@Moljesdev99](https://github.com/Moljesdev99))**

A **free, general-purpose web watcher**. It runs on [GitHub Actions](https://docs.github.com/en/actions), checks the pages you care about every 1 to 2 minutes, and messages you on [Telegram](https://telegram.org/) when something changes — a product comes back in stock, a price drops, tickets go on sale, a release date appears, a hotel is no longer sold out, or any other text on a page appears, disappears, or changes. You can also receive the same alerts on [ntfy](https://ntfy.sh/).

It is not limited to shop restocks. Restocks are one watch type among several.

## What it does

- Watches one or more web pages, day and night, even if your computer is off.
- Sends a **Telegram card** with a photo (when one is available), a short status, and a button: **🛒 View product** for restocks, **🔗 Open page** for everything else.
- Optionally sends the same alert to ntfy.
- Every day at **09:00 Mexico City time** (`America/Mexico_City`), it sends a heartbeat, as long as the primary restock item is not in stock.
- This copy stops on its own on **21 November 2026** (a date hard-coded in the source; see below to change it).

The **restock** type is tested with **OpenAI Supply Co.** and the **Disney Store** (including Disney’s Queue-it waiting room). The other types (`text_appears`, `text_disappears`, `price_below`, `changed`) work when the information is present in the HTML that the watcher downloads. See [Limitations](#limitations).

## How it works

```
GitHub Actions
  (every 20 minutes a run of about 5 h 40 min starts)
        │
        ▼
  The program fetches each watched page
  (an HTTPS request that impersonates Chrome)
  and checks again every 60–120 seconds
        │
        ├─ Stock, price, text, or content matches your rule
        │  (or a virtual waiting room opens, or the page cannot be read)
        │         ▼
        │    Telegram  (+ ntfy, if you configured it)
        │
        └─ Once a day, at 09:00 Mexico City time:
              “☁️ Cloud watcher is active”
```

It does not use AI and does not spend tokens. Bot credentials are stored as **GitHub Actions secrets**, not in the code.

The first successful check of a generic watch only records a baseline and does **not** send an alert. You are notified on the next change (text appears or disappears, the price falls to or below your threshold, or the content hash changes). Set an optional `cooldown` (seconds) if you also want reminder alerts while the condition stays true.

## Watch types

Existing restock entries keep working if you omit `type` (it defaults to `restock`). You can put everything in `products`, or add extra entries under `watches` — both lists are merged.

| `type` | Triggers when | Useful for |
|---|---|---|
| `restock` | The page looks in stock, the expected date changes, it sells out again, or (Disney) a `queue.*` waiting room opens | Products |
| `text_appears` | A string or regex is found on the page (optional CSS `selector`) | “Buy tickets”, a release date, “Available” |
| `text_disappears` | A string or regex is no longer found | “Sold out” disappearing on a hotel or event page |
| `price_below` | An extracted price is at or below `threshold` (CSS selector, regex, or JSON-LD) | Flights, hotels, products — **if the price is in the HTML** |
| `changed` | The text of the page (or of a `selector`) changes compared with the last check | Release-date pages, any “tell me if this changes” use |

Common fields on every entry: `id`, `name`, `url`, `type`, `emoji`, optional `image` / `image_url`, optional `cooldown` (seconds), optional `is_test` (prefix alerts with 🧪 Test), optional `primary` (daily heartbeat; only one should be true), optional `button_text`. Parameters can go in a `params` object or directly on the entry (`text`, `pattern`, `selector`, `threshold`, `currency`, `jsonld`, `ignore`, `inclusive`).

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
  "primary": true
}
```

`short_name` is used in “the Chromatic Codex is in stock!”.

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
| 🟢 … is in stock! | Restock: the product is available. Repeated every 2 minutes, up to 10 times |
| 📅 Restock date changed | The page now shows a different expected date |
| ⚪ Sold out again | It was in stock and sold out again |
| 🚦 The virtual waiting room is open! Join now | Restock: the page redirected to a `queue.*` host (as Disney does). Up to 3 alerts, 3 minutes apart |
| 🟢 “Buy tickets” is now on the page | `text_appears` |
| 🟢 “Sold out” is no longer on the page | `text_disappears` |
| 🟢 Price is US$180.00 (threshold US$200.00) | `price_below` |
| 🔔 The page content changed | `changed` |
| ⏰ Restock is today | Primary restock product only, on 19 November 2026 from 07:00 Mexico City time |
| ☁️ Cloud watcher is active | Daily heartbeat (if the primary restock item is not in stock) |
| ⚠️ I could not read … N times in a row | 5 consecutive failures (possible block). It keeps retrying, with longer waits |
| ✅ Watcher recovered | It could read the page again after that block |

If an entry has `"is_test": true`, its alerts are prefixed with **🧪 Test**.

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

This repository’s live `config.json` includes the two restock products the project was originally tested with (Chromatic Codex and the Disney lightsaber). Replace them with yours when you use your own copy.

### 7. Enable GitHub Actions and run a test

1. In your repository, open the **Actions** tab.
2. If GitHub asks you to confirm that you trust the workflows, accept.
3. In the left-hand list, open **Restockr**.
4. Click **Run workflow**, choose `test`, and confirm.

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
| Pause between checks | `CHECK_MIN_S` and `CHECK_MAX_S` in `restockr_core.py` | 60 to 120 seconds |
| How often GitHub starts a run | `cron` in `.github/workflows/restockr.yml` | minutes 7, 27, and 47 of every hour |
| Length of each run | `RUN_MINUTES` in the workflow | 340 minutes (~5 h 40 min) |
| Daily heartbeat | `HEARTBEAT_HOUR` and `HEARTBEAT_MINUTE` in `restockr_core.py` | 09:00, `America/Mexico_City` |
| “Restock is today” alert | `NOV19` in `restockr_core.py` | 19 November 2026, 07:00 Mexico City time, primary restock product only |
| Automatic stop | `END_AT` in `restockr_core.py` | 21 November 2026, 23:59 Mexico City time |

The GitHub cron does **not** check the page by itself: it only starts a run. Inside the run, the program checks every 1 to 2 minutes. If a run is still going, the next one waits in line (they do not cancel each other). If you want it to keep going after 21 November 2026, change `END_AT`.

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
- If `END_AT` has passed, the program starts and exits immediately. Change that date if you want it to continue.

## Limitations

- **GitHub Actions delays.** The `cron` schedule is not second-accurate; GitHub may lag.
- **GitHub terms of use.** Actions is intended mainly for software-development work (tests, publishing code, and similar). Using it as a personal always-on service may fall outside that, and GitHub could limit or stop the workflow. Use it at your own risk.
- **Bot protection and JavaScript.** The watcher downloads HTML with a Chrome-like request. It does not run a full browser session. Sites with Cloudflare, captchas, virtual queues, or pages that only fill in stock/price after JavaScript may fail. It warns after 5 consecutive failures and keeps retrying.
- **What was actually tested.** Restock detection is tuned for OpenAI Supply Co. and the Disney Store (including Queue-it when the final host starts with `queue.`). Generic types were tested with simulated HTML, [example.com](https://example.com/) (`text_appears`, `text_disappears`, `changed`), and a public HTML shop ([books.toscrape.com](https://books.toscrape.com/)) for `price_below`. **Google Flights, airline sites, major hotel/booking sites, and streaming catalogs were not verified** and often will not work. Prefer pages that already show the information in the HTML, official release-date pages, or a documented API.
- **Built-in end date.** This copy stops on 21 November 2026 until you change `END_AT`.
- **State lives in the Actions cache.** It is not in the repository (and should not be committed). If the cache is lost, the watcher starts again from `state.seed.json` or from a blank state.

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
