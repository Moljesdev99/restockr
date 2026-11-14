# Restockr

**Created by Jesús Molina ([@Moljesdev99](https://github.com/Moljesdev99))**

A **free** restock watcher. It runs on [GitHub Actions](https://docs.github.com/en/actions) (machines GitHub lends you at no charge), checks product pages every 1 to 2 minutes, and messages you on [Telegram](https://telegram.org/) when an item is in stock, the expected date changes, or — on Disney — a virtual queue opens. You can also receive the same alerts on [ntfy](https://ntfy.sh/).

## What it does

- Checks one or more product pages, day and night, even if your computer is off.
- Sends a **Telegram card** with a photo, status, price, expected date (when the page shows one), and a **🛒 Ver producto** button.
- Optionally sends the same alert to ntfy.
- Every day at **09:00 Mexico City time** (`America/Mexico_City`), it sends a heartbeat, as long as the primary product is not in stock.
- It stops on its own on **21 November 2026** (a date hard-coded in the source; see below to change it).

It is built mainly for **OpenAI Supply Co.** and the **Disney Store**. On other shops it may work if the page uses typical “out of stock” or “add to cart” wording or buttons, but that is not guaranteed.

## How it works

```
GitHub Actions
  (every 20 minutes a run of about 5 h 40 min starts)
        │
        ▼
  The program fetches each product page
  (an HTTPS request that impersonates Chrome)
  and checks again every 60–120 seconds
        │
        ├─ In stock, date changed, virtual queue opened,
        │  sold out again, or the page could not be read
        │         ▼
        │    Telegram  (+ ntfy, if you configured it)
        │
        └─ Once a day, at 09:00 Mexico City time:
              “☁️ Vigilante en la nube activo”
```

It does not use AI and does not spend tokens. Bot credentials are stored as **GitHub Actions secrets**, not in the code.

## Alerts you may receive

| Alert | Meaning |
|---|---|
| ☁️ Restockr en la nube: activo | Startup or test run: whether each product page could be read |
| 🟢 ¡Ya hay stock! | The product is available. Repeated every 2 minutes, up to 10 times |
| 📅 Cambió la fecha de restock | The page now shows a different expected date |
| ⚪ Se agotó de nuevo | It was in stock and sold out again |
| 🚦 ¡Abrió la fila virtual! Entra ya | The page redirected to a `queue.*` waiting room (as Disney does). Up to 3 alerts, 3 minutes apart |
| ⏰ Hoy es el restock | Primary product only, on 19 November 2026 from 07:00 Mexico City time |
| ☁️ Vigilante en la nube activo | Daily heartbeat (if the primary product is not in stock) |
| ⚠️ Could not read the product N times in a row | 5 consecutive failures (possible block). It keeps retrying, with longer waits |
| ✅ Vigilante recuperado | It could read the page again after that block |

If a product has `"is_test": true` in the config, its alerts are prefixed with **🧪 Prueba**.

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

### 6. Put your products in `config.json`

In your repository, open `config.json` → the pencil icon. You can start from [`config.example.json`](config.example.json). Use a list like this:

```json
{
  "products": [
    {
      "id": "my-product",
      "name": "Full product name",
      "short_name": "the product",
      "url": "https://store.example.com/product",
      "emoji": "🎮",
      "is_test": false,
      "primary": true
    }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | Yes | Short unique id, no spaces (example: `chromatic`) |
| `url` | Yes | Product page URL |
| `name` | No | Name shown on the card. Defaults to `id` |
| `short_name` | No | Used in “¡Ya hay stock de …!”. Defaults to `name` |
| `emoji` | No | Card emoji. Defaults to 🎮 |
| `is_test` | No | `true` marks alerts as 🧪 Prueba. Defaults to `false` |
| `primary` | No | Set `true` on **one** product: it receives the daily heartbeat and the 19 November heads-up. If none is set, the first item in the list is used |

You can add more objects inside `products`. Then click **Commit changes**.

This repository’s `config.json` includes the two products the watcher was tested with (Chromatic Codex and the Disney lightsaber). Replace them with yours.

### 7. Enable GitHub Actions and run a test

1. In your repository, open the **Actions** tab.
2. If GitHub asks you to confirm that you trust the workflows, accept.
3. In the left-hand list, open **Restockr**.
4. Click **Run workflow**, choose `test`, and confirm.

Within a minute or two you should get **☁️ Restockr en la nube: activo** on Telegram, with each product’s status. If that arrives, run the workflow again with `watch` so it keeps checking. The scheduled cron will also start it on its own.

## Adding or removing products

Edit `config.json`, add or delete entries in `products`, and **Commit**. The change applies on the next run.

You do not need to edit `state.seed.json`. That file is only an initial state the first time (or if GitHub drops memory between runs). If you switch products, you can leave it or delete it; new products start with a blank state.

## Customizing frequency and dates

These values live in the code, not in `config.json`. Change them in a commit if you need something different:

| What | Where | Current value |
|---|---|---|
| Pause between checks | `CHECK_MIN_S` and `CHECK_MAX_S` in `restockr_core.py` | 60 to 120 seconds |
| How often GitHub starts a run | `cron` in `.github/workflows/restockr.yml` | minutes 7, 27, and 47 of every hour |
| Length of each run | `RUN_MINUTES` in the workflow | 340 minutes (~5 h 40 min) |
| Daily heartbeat | `HEARTBEAT_HOUR` and `HEARTBEAT_MINUTE` in `restockr_core.py` | 09:00, `America/Mexico_City` |
| “Hoy es el restock” alert | `NOV19` in `restockr_core.py` | 19 November 2026, 07:00 Mexico City time, primary product only |
| Automatic stop | `END_AT` in `restockr_core.py` | 21 November 2026, 23:59 Mexico City time |

The GitHub cron does **not** check the product by itself: it only starts a run. Inside the run, the program checks every 1 to 2 minutes. If a run is still going, the next one waits in line (they do not cancel each other). If you want it to keep going after 21 November 2026, change `END_AT`.

## How to turn it off

In your repository: **Actions** → **Restockr** → the **…** menu → **Disable workflow**.

## Troubleshooting

**I did not get the test message**
- Confirm the secrets are named exactly `TG_TOKEN` and `TG_CHAT`.
- Confirm you sent `/start` to your bot.
- Open **Actions** → the latest run → the **Watch** step and look for an error (for example “Missing TG_TOKEN / TG_CHAT”).

**The message says it could not read a page**
- The shop may be blocking the watcher (Cloudflare or similar).
- The page may depend on heavy JavaScript, or may not use wording/buttons the program recognizes.
- Try an OpenAI Supply or Disney Store page first; those are the ones already verified.

**The in-stock alert arrived late**
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
- **Bot protection.** Shops with Cloudflare, captchas, or other barriers may block the checker. The program warns after 5 consecutive failures and keeps retrying.
- **Not a universal monitor.** Detection is tuned for OpenAI Supply Co. and the Disney Store (including the Queue-it waiting room, when the final host starts with `queue.`). On other sites the result is uncertain.
- **Built-in end date.** This copy stops on 21 November 2026 until you change `END_AT`.
- **State lives in the Actions cache.** It is not in the repository (and should not be committed). If the cache is lost, the watcher starts again from `state.seed.json` or from a blank state.

## Privacy

- **Never commit** the bot token, chat id, or ntfy topic to the repository, and do not paste them into issues or the README.
- Store them only as GitHub secrets (**Settings** → **Secrets and variables** → **Actions**).
- If the repository is public, other people can see your `config.json` (product names and URLs). They cannot see the secrets.
- The ntfy topic works like a password: choose a long one and do not publish it.
- [`.gitignore`](.gitignore) ignores `state.json`, logs, and typical local credential files (`.tg_token`, `.tg_chat`, `.env`).

## License

[MIT](LICENSE). Copyright © 2026 Jesús Molina.

---

**Created by Jesús Molina ([@Moljesdev99](https://github.com/Moljesdev99))**
