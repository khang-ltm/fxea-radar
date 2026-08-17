# FX EA Radar

Scrapes a Telegram channel (default `@free_fx_chat`) for free-EA posts, parses each
message into structured fields (EA name, pairs, timeframe, min deposit, leverage,
broker, rules/settings, download links, attached files), and serves a local website
where you can search, filter, and spot what is new since your last visit.

Python + Telethon (MTProto). No Docker, no database, no cloud.

## Why MTProto and not plain HTML scraping

`@free_fx_chat` has no public web preview (`t.me/s/free_fx_chat` returns zero
messages), so the HTML-scraping route is dead. Telethon logs in as *your own
account* — the same account that already reads the channel — and pulls history
through the real Telegram API.

## Setup (one time)

1. Get API credentials: https://my.telegram.org → **API development tools** →
   create an app → copy `api_id` and `api_hash`.
2. In this folder:

   ```powershell
   Copy-Item .env.example .env
   notepad .env          # paste TG_API_ID + TG_API_HASH, check TG_CHANNELS
   .\run.ps1 install     # creates .venv and installs telethon
   .\run.ps1 login       # asks phone number, Telegram login code, 2FA password
   ```

   Your Telegram account must already be a member of the channel.

3. First scrape + open the site:

   ```powershell
   .\run.ps1 sync        # walks back FIRST_SYNC_LIMIT messages (default 800)
   .\run.ps1 serve       # http://127.0.0.1:8787
   ```

Later runs: `.\run.ps1 serve` is enough — the server auto-syncs every
`SYNC_INTERVAL_MIN` minutes, and the **Sync now** button forces one immediately.
Each sync is incremental (only messages newer than the last one seen).

Without the helper script:

```powershell
.venv\Scripts\python.exe -m app.login
.venv\Scripts\python.exe -m app.sync
.venv\Scripts\python.exe -m app.server
```

## The website

- Cards newest-first, purple border + `NEW` badge for anything posted after your
  last **Mark all seen** click (stored in browser localStorage).
- Full-text search over name, rules, raw message, filenames, pairs, tags, broker.
- Filters: EA-only, has-file, unseen-only, pair, timeframe, and tag chips
  (`scalper`, `martingale`, `grid`, `prop-firm`, `mt4`, `mt5`, `cracked`, …).
- Sort by newest / oldest / most EA-like / most views.
- Each card shows extracted rules and settings, deposit/leverage/lot/risk/broker/
  account type/password/expiry when present, links, attached files with a local
  download link, plus the raw message in a collapsible block.

## Config (`.env`)

| Key | Meaning |
| --- | --- |
| `TG_API_ID`, `TG_API_HASH` | from my.telegram.org |
| `TG_CHANNELS` | comma-separated usernames / `t.me` links / `-100…` ids |
| `FIRST_SYNC_LIMIT` | messages pulled on the first sync per channel (default 800) |
| `DOWNLOAD_FILES` | `true` to save attachments to `data/files/` |
| `MAX_FILE_MB` | skip attachments larger than this (default 25) |
| `PORT` | web server port (default 8787) |
| `SYNC_INTERVAL_MIN` | background auto-sync interval; `0` disables |

## Layout

```
app/config.py   .env loading, paths
app/login.py    one-time interactive Telegram login
app/parse.py    message -> structured EA fields (all the regex work)
app/sync.py     fetch messages + attachments, merge into the store
app/store.py    JSON store (data/posts.json, data/state.json)
app/server.py   stdlib HTTP server + JSON API
public/index.html   single-page dashboard
data/           posts.json, state.json, tg.session, files/   (all gitignored)
```

API: `GET /api/posts`, `GET /api/state`, `POST /api/sync`, `GET /files/<path>`.

## Parsing accuracy

Channel posts have no schema, so extraction is heuristic and tuned to common
formats (`Min deposit: $500`, `1:500`, `Timeframe: M15`, numbered/bulleted rule
lists, `RULES:` headings). Missing fields simply do not render. `is_ea` is a
keyword+attachment score (`ea_score >= 3`) that keeps chat and signal spam out of
the default view — flip **EA only** off to see everything stored.

To improve extraction for this channel's specific style, edit the regexes in
`app/parse.py`; re-running `sync` re-parses only new posts, so delete
`data/posts.json` and `data/state.json` if you want everything re-parsed from
scratch (a full re-download).

## Free hosting: GitHub Actions + Pages (works while your PC is off)

`.github/workflows/sync.yml` runs the sync on GitHub's machines twice a day
(08:00 and 20:00 Vietnam time), rebuilds a static copy of the dashboard into
`site/`, and publishes it to GitHub Pages. No server, no cost.

What the static build gives up: attachments are not hosted (a static host has no
`/files` route), so every download button links to the Telegram post instead.
Everything else - grouping, verdicts, Myfxbook links, filters, detail sheet - is
the same page, generated from `public/index.html` so there is no second UI.

Setup, once:

```powershell
python -m app.export_session      # prints a StringSession for CI
```

Then in the GitHub repo → Settings → Secrets and variables → Actions:

| Secret | Value |
| --- | --- |
| `TG_API_ID` | from my.telegram.org |
| `TG_API_HASH` | from my.telegram.org |
| `TG_SESSION` | the string printed above |

Settings → Pages → Source: **GitHub Actions**. Then run the workflow once from the
Actions tab. The site appears at `https://<user>.github.io/<repo>/`.

`data/posts.json`, `data/shots.json` and `data/state.json` are tracked on purpose:
the workflow commits them back after each run so the next sync is incremental
instead of re-reading the channel from scratch. `data/files/`, `.env` and
`data/tg.session` stay ignored.

Preview the static build locally:

```powershell
python -m app.export_static      # writes site/index.html
```

### Before making the site public

- A public Pages site is readable by anyone with the URL. It exposes EA names,
  rules, verdicts and Telegram links - not your session, but not private either.
  For access control, publish to Cloudflare Pages instead and put Cloudflare
  Access in front (both free).
- `TG_SESSION` in repo secrets means a Telegram login lives in GitHub. Prefer a
  throwaway Telegram account joined only to the channels you scrape.
- Never commit `.env` or `data/tg.session`. Both are gitignored; keep it that way.

## Security notes

- `data/tg.session` **is** an authenticated login to your Telegram account.
  Anyone with that file can read your messages. Never commit or share it.
- Downloaded `.ex4/.ex5` files are unverified third-party binaries. Free-EA
  channels are a known malware and account-stealer vector. Run them only in a
  throwaway MT4/MT5 install on a demo account, ideally inside a VM.
- Cards tagged `cracked` are pirated commercial EAs; the tag exists so you can
  filter them out.
- Reading a channel you have joined is normal client behaviour, but mass
  automated downloading can trip Telegram rate limits. Keep
  `SYNC_INTERVAL_MIN` at 15 or higher.
