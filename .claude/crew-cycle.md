# The daily crew cycle

What the automatic crew does once a day, unattended, in a cloud session with a
fresh checkout of this repo. Read CLAUDE.md first; everything here assumes it.

## Human gates - never cross these

The crew works alone, so these are not suggestions. Each one is a place where a
person decides, and the crew stops and hands over.

1. **Merging to `main`.** A push to `main` deploys twice: GitHub Actions
   republishes the site, and the agent on the VPS updates itself from `main` -
   on an account with real money. The crew works on a branch and opens a pull
   request. It never pushes to `main`, never merges, never force-pushes, never
   edits `.github/workflows/`.
2. **Trades.** Nothing the crew writes may open, close or cancel anything, or
   change the trade path. `tests/e2e.py` checks the trade path; a PR that needs
   that check changed is the wrong PR.
3. **EA settings.** No code that writes an EA's inputs, applies a template or
   offers a button that does. Displaying settings is fine.
4. **The VPS, the provider, the accounts.** The crew has no token, no SSH and no
   Telegram login in the cloud, and must not ask for any or put one in a file.
5. **Anything marked human-only** on the board or in an issue - the daily-loss
   guard is the standing example. Do not scope it, build it or suggest a default.

If the only useful work left today sits behind a gate, say so in the report and
stop. A day with no PR is a correct outcome.

## The cycle

**1. Stand-up (BA + LEAD).** Run the checks and read the state:

```bash
pip install -r requirements.txt
python tests/e2e.py
node tests/pagesmoke.mjs
git log --oneline -15
```

Pick **one** piece of work, in this order of preference:
- a failing check
- an open item in "Known gaps" below
- a defect found by reading real data in the repo (the catalog store is not in
  the repo; `tests/` fixtures and `data/` files that are tracked are)

Only work that can be checked against something real. If nothing qualifies,
skip to the report.

**2. Build (FE or BE).** On a branch named `crew/YYYY-MM-DD-<slug>`:
- one change, small enough to review in five minutes
- patch `public/index.html` with a script written through the Write tool, never
  a heredoc (they collapse backslashes - see CLAUDE.md)
- never invent a number, a label or an option mapping; unknown stays unknown

**3. QA.** Run both test suites. Add an assertion for what changed, then break
the code and confirm the assertion fails. Revert the break. If anything in the
diff touches `order_send`, `TRADE_ACTION`, `setinputs`, `ChartApplyTemplate`,
`closeposition` or `cancelpending`, stop and do not open the PR.

**4. Ship to the gate.** Push the branch and open a pull request against `main`
titled `crew: <what changed>`. The body says:
- what was wrong, with the evidence (a failing check, a real row)
- what changed and what was deliberately left alone
- how it was tested, including the mutation that made the new assertion fail
- end with: `Human gate: review and merge to deploy. Merging redeploys the VPS agent.`

**5. Report.** Finish with a short stand-up written as the crew - one line per
agent who did something, plus a "waiting on you" line listing the PR and any
gated items. This text is the day's meeting; it is copied onto the Radar Crew
board.

## Known gaps

Small, checkable, not behind a gate. Remove an item when its PR merges.

- `app/tg_evidence.py` is unwired on purpose: public Telegram channels turned
  out to be sales channels. Leave it unwired.
- Quantum Bitcoin's drawdown self-check is off by $30.28 (BTCUSD contract size
  is the suspect) - `app/basket_dd.py`.
- Smart Gold Hunter's drawdown self-check is off by $11.56.
- The MQL5 Market crawl reaches only the first ~70 listings: later pages
  lazy-load through XHR.
- Settings that differ from the author's shipped `.set` are not marked yet.
  Display only - never a reset button.
