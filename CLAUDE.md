# fxea-radar

A control panel for a live MT5 account. The EA catalog (free EAs scraped from
Telegram) and the MQL5 Market tab were shut down on 2026-10-23: their code is
kept, dormant, and nothing calls it or shows it. Two halves that ship differently:

| half | lives in | how it ships |
|---|---|---|
| the site | `public/index.html` | GitHub Actions builds `site/` on every push to `main` and publishes it |
| the agent | `app/mt5_agent.py` | runs on a Windows VPS, updates itself from `main` |
| the manager | `mql5/FxeaManager.mq5` | the agent compiles it; MT5 reloads it by itself |

Live site: https://fxea-radar.linkpc.net · agent: https://mt5.fxea-radar.linkpc.net

## Hard rules

**Real money is attached to this.** The agent can close positions, cancel
orders and set SL/TP on open positions on an account that trades. Every trade
request must be a `TRADE_ACTION_REMOVE`, a `TRADE_ACTION_DEAL` naming an
existing `position`, or a `TRADE_ACTION_SLTP` naming an existing `position`.
Nothing in this repo may open a trade. `tests/e2e.py` enforces this.
Nothing can open a trade or add volume; with the PIN, SL/TP on an open position
can be changed, including widening or removing a stop - that is the owner's
accepted risk (2026-10-22).

**Never invent a number.** Everything shown is either measured, read from a
source that is named on screen, or absent. An unknown is displayed as unknown,
never as zero and never as a plausible guess. Two examples that cost real time:
a drawdown model that priced every long as a short looked perfectly reasonable
until it was checked against trades whose results were already known, and an
option list guessed for `InpPause` would have displayed the exact opposite of
the truth.

**Check against the live account before believing a rule.** The agent token
reads real charts, real inputs, real history. A rule that looks right in the
abstract is worth nothing next to one entry it gets wrong. Most bugs in this
repo were found by printing real data, not by reasoning.

**Say what a label is worth.** Labels come from four places and outrank each
other in this order: a note written by the user, the EA's own `.mq5` source,
its published manual, a naming convention. A derived name - the variable spelled
out in words - claims nothing and is shown plain.

## Layout

```
app/mt5_agent.py    the VPS agent: MT5, HTTP API, self-update  (large)
app/basket_dd.py    drawdown reconstruction from M1 bars
app/export_static.py builds site/index.html, baking in the agent hostname
public/index.html   the whole front end, one file, no build step: My MT5 + My EA
mql5/FxeaManager.mq5 sits on a spare chart, reads other charts' inputs
```

Dormant since 2026-10-23 (kept so they can be revived, not called by anything
that ships): `app/sync.py` and `app/sync_shots.py` (Telegram catalog sync),
`app/mql5_market.py` (MQL5 Market crawler), `app/server.py` (local catalog
server + JSON API), `build_payload()` in `app/export_static.py`, and
`tests/fixtures/market_sample.json`. The catalog and Market front end was
removed from `public/index.html`; it is in git history before that date.

## Before saying anything is done

```bash
.venv/Scripts/python.exe tests/e2e.py      # agent, API, trade-path, control chars
node tests/pagesmoke.mjs                   # the page runs and renders
.venv/Scripts/python.exe -m app.export_static
```

A test that passes against a deliberately broken version is not a test. When
adding one, break the thing it covers and watch it fail - an assertion here once
passed against a mutant because the fixture's own text contained the number it
was looking for.

## Things that will waste your time

- `public/index.html` is 4,300 lines and has no build step. Patch it with a
  script, not by hand. Heredocs mangle backslashes: write patch scripts with the
  Write tool, and build regex escapes with `chr(92)`.
- The agent's self-restart does not reliably work on that VPS. After deploying,
  check `/api/selfcheck` - "running X but Y is on disk" means it did not take.
- An update only copies `app/`, `public/`, `mql5/` and the root `.ps1` scripts.
- mql5.com returns 403 to every server request. Pages go through `r.jina.ai`.
- `.ex5` files are encrypted (7.99 bits of entropy per byte). Input labels
  cannot be recovered from them. Version, author and link are readable at fixed
  offsets 168, 424, 680.
