# goat — Surya's internship application agent

Implementation of the plan "Swap in OpenAI, keep Playwright, skip Stagehand". One asyncio process,
one SQLite file, one Telegram bot. **Only gpt-6-luna is used** (Surya's decision after gpt-5.6-sol burned
about $70 in the Hermes attempt): executor at low effort, advisor at medium, classifier at none. The
allowlist `ALLOWED_MODELS=gpt-6-luna` is enforced in code (`goat.config.assert_allowed`) on every request
path and at import time, so no setting can route a call to Sol or Astra. The model drives a dedicated Chrome
through Playwright over CDP. Passwords, emailed codes,
the 15-minute timer, the never-apply-twice ledger and the final submit are deterministic code the
model can call but cannot bypass. No CAPTCHA is ever solved: a challenged form is filled and parked
one click from done.

Design notes: [docs/DESIGN.md](docs/DESIGN.md). Environment record: [SETUP.md](SETUP.md).

## State of the build (2026-10-15)

| Piece | Status |
|---|---|
| SQLite schema, states, answer store (329 live answers, 197 aliases), ledger (249 applications, 397 keys from Hermes) | done, imported |
| Triage + scraper (SimplifyJobs diff, ETag, liveness checks for Greenhouse/Lever/Ashby) | done, tested live (read-only) |
| Browser: Chrome launcher, guarded executor, snapshot with refs, error scan, fill/choose/upload, CAPTCHA detection, `submit()` | done, offline tests pass |
| Gate (errors, required, sources, attestations, résumé, ledger, prohibitions, guardrails, shadow/live) | done |
| Responses API loop, 15 function tools, safety-check policy, advisor, classifier | done; Phase-0 API checks pass on gpt-6-luna (computer tool, strict JSON at effort none, screenshot `detail: original`, `configuration_update`); real-model shadow run on the Workday fixture: 3 pages filled, asked for the attestation instead of guessing |
| Telegram bot (owner-only, forced replies, buttons, `/remember`, `/status`), ask/park/release transaction | done, tests pass; needs a token |
| Gmail code/link helper, Bitwarden CLI helper | done, tests pass; need OAuth client + `bw` |
| Worker, supervisor, watchdog, digest, CLI | done |
| Adapters: Greenhouse, Ashby, Lever (ready_for_you lane), Workday, Oracle, iCIMS (hints + prefill) | done, untested on live portals |
| Proposals / nightly evidence job | not built (Phase 5) |

Tests: `python -m pytest -q` → 59 passed (including the Workday-style workflow and the dashboard).

**First live submission (2026-10-15):** National Life, Data Science Intern (Greenhouse), submitted by goat with
gpt-6-luna after Surya reviewed the draft. Five attempts on the real form exposed and fixed: the invisible
reCAPTCHA badge counted as a challenge; file inputs labelled "Attach" hiding the résumé slot; a "Yes" answer
landing on "Not willing to relocate"; invented values under a free "adapter" source; stale values from a prior
attempt with no source; an ambiguous school prefix picking the wrong campus; the model parking instead of
re-sourcing. Each is now a code rule (gate, prefill verification, matcher) rather than a prompt line. The
emailed security code was fetched from Gmail by the operator this once; the Gmail helper does it unattended
once `goat gmail-auth` has run. Total model cost for the listing: about $0.05.

**Review rule:** everything else stays in shadow mode (`GOAT_MODE=shadow`, `LIVE_PORTALS` empty). Each job's
draft is in the dashboard job drawer and `records/<job>/decisions.md`; Surya reviews drafts before any portal
is switched live. A reviewed draft is submitted with `python -m goat release <job>` (or the chat's `release`, or the website's
Release button); nothing else leaves shadow mode. Answers the agent still needs: [docs/QUESTIONNAIRE.md](docs/QUESTIONNAIRE.md).

**Verified auto-submit** (`AUTO_SUBMIT=verified`, Surya 2026-10-18): a draft is submitted on its own only when every
check agrees the information is correct: the code gate, the read-back on every page (no field shows something other
than its decision), no unanswered question, and the judge's pass with a fact check of every unconfirmed answer (essays
the agent wrote, derived answers, defaults) against `config/profile.md`. Anything short of that holds as
`review_needed`, and Telegram sends a "Needs your OK" message with a Submit button (a reply "submit" works too).
`goat run-once --shadow` never auto-submits; iCIMS is not run (`SKIP_ATS`).

## What you need to add before testing (all in `.env`)

1. `OPENAI_API_KEY`. Then run the Phase-0 API checks (disputed behaviours: computer tool per model,
   encrypted reasoning with `store=false`, `configuration_update`, screenshot `detail`):
   ```bash
   .venv\Scripts\python.exe -m goat check-api
   ```
2. Telegram: create a bot with @BotFather, put the token in `TELEGRAM_BOT_TOKEN`, send the bot a
   message, and set `TELEGRAM_OWNER_CHAT_ID` to your chat id. Without it, questions time out and hold.
3. Gmail (Phase 1/3): an OAuth client JSON (Desktop app, **Production** status, scope gmail.readonly)
   at `secrets/gmail_client.json`, then `python -m goat gmail-auth` once.
4. Bitwarden CLI (Phase 3): install `bw`, log in to a vault that holds only portal credentials, and set
   `BW_SESSION` (or `BW_PASSWORDFILE`). Then add `account` to `LANES_ENABLED`.
5. Chrome profile: the old Hermes Chrome still holds port 9222 with its signed-in profile. Either stop it
   and point `CHROME_USER_DATA_DIR`/`CDP_PORT` at that profile and port, or let goat start a fresh profile
   on 9223 (`python -m goat chrome`) and sign into Bitwarden there once.

## Desktop app

Double-click [goat.cmd](goat.cmd) (or run `npm start` in `app/`). The Electron shell starts the Python
supervisor (`python -m goat run`: scraper, worker, Telegram bot, mail reconciler, watchdog, dashboard) and
shows the dashboard in a window; closing the window stops the agent. If a `goat dashboard` or `goat run` is
already answering on the port, the shell attaches to it instead. Menu (goat ▸): Reload, Restart agent,
Open records folder, Open .env, Show agent log. `npm run dist` in `app/` builds a portable .exe (needs
`npm i -D electron-builder` once).

**Settings tab**: edit the OpenAI API key, models (allowed list locked to gpt-6-luna), mode, live portals,
lanes, budgets, Telegram, Bitwarden path/session and the Chrome port; saved to `.env`, applied on Restart agent.
It also shows integration status (Gmail token, Bitwarden vault state, Chrome CDP).
**Usage tab**: requests and spend for 1h / 24h / 7d / all, per model, per day, per job.

## Gmail API (emailed codes and verification links)

Workday, Oracle and iCIMS send a verification link or code when an account is created; Greenhouse sends
an 8-character security code at submit. goat reads only that mail, only from the portal being applied to.
One-time setup (about five minutes):

1. In Google Cloud Console create a project, enable the **Gmail API**, and under APIs & Services → OAuth
   consent screen choose External, add your application email (APPLICATION_EMAIL) as a test user, then **publish** the app
   (a client left in Testing gets refresh tokens that expire after seven days).
2. Credentials → Create credentials → OAuth client ID → Application type **Desktop app**. Download the
   JSON and save it as `secrets\gmail_client.json`.
3. Authorize once (opens a browser; grant read-only mail access):
   ```bash
   .venv\Scripts\python.exe -m goat gmail-auth
   ```
   The token lands in `secrets\gmail_token.json`; Settings → Integrations then shows Gmail "authorized".

## Bitwarden CLI (account lane)

The CLI is installed under the project (`js
ode_modules\.binw.cmd`, version 2026.10.0) and `.env`
points `BW_EXE` at it. One-time authentication, in a terminal (the master password never goes through
goat; the agent only ever sees "credentials filled"):

```bash
js
ode_modules\.binw.cmd login
```
```bash
js
ode_modules\.binw.cmd unlock --raw
```

Paste the printed session key into Settings → "Bitwarden session key" (or `BW_SESSION` in `.env`), add
`account` to Lanes enabled, and Restart agent. The memo recommends a separate Bitwarden account that holds
only portal credentials, because an unlocked vault is readable by anything running as this Windows user.
`bw lock` or a reboot invalidates the session; `BW_PASSWORDFILE` lets the supervisor unlock unattended.

## Dashboard

```bash
.venv\Scripts\python.exe -m goat dashboard
```

Opens http://127.0.0.1:8787 (one stdlib HTTP server, no extra dependencies; also started by `goat run`
and by the desktop app). Six tabs:

- **Now** — what the agent is doing this second: the job it claimed, the step it is on ("filling fields
  — 7 set, 0 failed"), elapsed time, steps, requests and cost, its last dozen actions, and a live feed
  across all jobs. Idle shows the next item in the queue. Below: counts (click one to filter Jobs) and
  anything waiting on you.
- **Jobs** — one table with filter chips (in flight / queued / needs you / submitted / all) and search.
  A row opens the draft: screenshots, every field with its source, questions, the full event log, and
  Requeue / Hold / Mark dead.
- **Questions** — every question asked, answered inline.
- **Answers** — the answer store. **Usage** — requests and spend by window, model, day and job.
  **Settings** — API key, models, mode, lanes, budgets, Telegram, Bitwarden, Chrome port, plus
  integration status.

Whenever the agent needs an answer, a **pop-up** appears over any tab with the question, one-tap option
buttons, a free-text box, and "I don't know yet". The header pill shows Working / Needs you / Idle.
Answering saves the answer under its key, so it is never asked again, and releases every job held on it.

## Cloud link (RDS, the website and Telegram on AWS)

The agent stays on this PC. Its only link to AWS is an RDS Postgres database, which the website and the
Telegram worker on one EC2 instance read. Both of those live in the goat-dashboard repo, with deploy
scripts in `deploy/aws/`.

With `GOAT_DB_URL` set in `.env`, `goat run` starts the link (`goat/cloud.py`):

- **Mirroring.** Every event, plus the jobs, listings, questions, answers, field fills, ledger rows and
  screenshots it touches, reaches RDS within about two seconds. A heartbeat goes up every 15 seconds.
  The local SQLite file stays the agent's working store, so a network outage never stops a run.
- **Commands.** Answers, holds, answer-bank edits, added listings, job actions, queue order, and
  pause / resume / stop / skip come back as commands. The agent applies them with the same code as the
  local dashboard (the question broker, `answers.save_answer`, `goat/intake.py`, `worker.CONTROL`).
- **Telegram.** The local Telegram bot stays off, because the worker on the web server sends questions,
  progress marks and outcomes.
- **Browser outages.** When Chrome does not answer (a CDP connect timeout), the job goes back to the queue
  without spending an attempt. The worker backs off for 1, 2, 5, 10, then 15 minutes and reports the
  outage once, so it no longer holds the whole queue with "error".

```bash
.venv\Scripts\python.exe -m goat cloud-sync
```

This mirrors everything once without starting the worker.

```bash
.venv\Scripts\python.exe scripts/cloud_smoke.py --env-file <file> --workdir <scratch>
```

This walks a fake job ("Simulator Co", no Chrome) through every progress mark against a copy of goat.db.

The old all-in-one cloud VM (Docker, Xvfb, noVNC) is in `legacy/vm/`. It is git-ignored and not used.

## Talking to goat on Telegram (the chat agent)

Any message to the bot that is not a reply to a question or a slash command is a conversation turn. The
Telegram worker on EC2 writes it to the `commands` table (kind `chat`); the cloud link on the PC hands it to
`goat/chat/` (a gpt-6-luna Responses loop with the system prompt in `goat/chat/soul.md`); the reply comes back
as a `chat:reply` event, which the worker sends as a threaded reply, with a screenshot when there is one.

What it can do: `status`, `list_jobs`, `job_detail`, `job_action` (run_now / requeue / hold / skip), `worker`
(pause / resume / stop_now / skip_current), `answer_question`, `keep_as_is` (you set the fields yourself in
Chrome: "I filled it, continue"), `add_listing` (paste a URL), `screenshot` and `page_outline` of a job's page,
`browser` (click / type / key / scroll / goto on a PARKED job's tab; a running job's tab is look-only),
`remember` (a fact about you with an answer key, or a note about the work), `read_skill`, `recent_events`.
It never submits, never touches a CAPTCHA, never sees a password or code.

Replying "I already filled it out" / "done" / "leave it as is" to a question message settles it as AS_IS: the
run records what the page shows (`fill_batch` source `as_is`) and carries on instead of parking.

## Skills and memory

- `skills/*.md`: procedures with a small front matter (`name`, `description`, `portals`, `agents`, `triggers`).
  The filling agent carries the skills for the job's portal in its cached prefix; the chat agent always has the
  index and pulls in the skills a message triggers. Local-only skills go under `data/skills/`. Skills hold
  procedures, never facts about Surya.
- `data/memory/MEMORY.md` (curated standing notes) and `data/memory/notes/YYYY-MM-DD.md` (one line per run
  outcome, plus notes added over chat). Both agents get a snapshot at the top of their prompts.

## Workday-style workflow test

`tests/fixtures/workday.html` imitates a Workday application: a rail of four pages, "Select One" button
dropdowns with `data-automation-id="selectWidget"` popups, a robots-only honeypot field, Save and
Continue on every page and a Review page whose primary button becomes Submit.
`tests/test_workday_workflow.py` drives the real code path (adapter prefill, `choose`, `fill_batch`,
`upload_resume`, `click`, gate, `submit`) with the model replaced by the tool calls it would make:
the honeypot stays empty, the Submit control is refused by the click tool, shadow mode parks before the
click, live mode submits once with a positive signal, and every decision carries a source.
`python -m goat demo [--live]` runs the same workflow against the real database so the dashboard shows
a complete run (job 196, listing `demo:workday-fixture`; the ledger is never written).

## Running

```bash
.venv\Scripts\python.exe -m goat status
```
```bash
.venv\Scripts\python.exe -m goat run-once --shadow
```
```bash
.venv\Scripts\python.exe -m goat run
```

`run-once --shadow` claims the next queued job, fills it, runs the gate and stops before the click;
the result and `records/<job>/decisions.md` show every field with its source. After ten clean shadow
fills on a portal, set `GOAT_MODE=live` and add the portal to `LIVE_PORTALS`.

Other commands: `list --status hold`, `show <job>`, `requeue <job>`, `hold <job> --reason`, `lookup
"<question>"`, `remember <key> <value>`, `scrape`, `digest`, `import-hermes --dry-run`, `seed`.

## Safety properties enforced in code

- Clicks from the model on submit controls or inside CAPTCHA frames are refused by the executor.
- `submit()` is the only path to submission; it refuses while the page scans dirty, commits a
  `submitting` marker, clicks once, and reports submitted / blocked / failed. No "unclear".
- Passwords and emailed codes never enter model context, logs, records or exceptions.
- Navigation only to the listing's host, its redirects, and known ATS hosts.
- Every filled field is recorded with a source; attestations only from standing answers.
- `pending_safety_checks`: malicious_instructions / sensitive_domain park the job; irrelevant_domain is
  acknowledged only on the allowlist.
- A reply to the bot saves the answer and releases every held job on that key in one transaction.

## Windows boot chain (Phase 0, not yet done)

Autologon (Sysinternals) → Task Scheduler "At log on", "Run only when user is logged on", restart on
failure → `python -m goat run`. Sleep off on AC. The supervisor holds `SetThreadExecutionState`.
