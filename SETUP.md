# bub — environment setup record (2026-10-14)

Nothing is built yet. This records what is installed, what was found on the machine,
and what is still missing before Phase 0 of the design memo can start.

## Layout
```
bub/
  .venv/              Python 3.12.13 (uv-managed; system default is 3.14, too new for several libs)
  requirements.txt    intent (loose pins)        requirements.lock  exact freeze
  js/                 npm project: @browserbasehq/stagehand 4.2.1, openai, playwright, zod
  .env.example        copy to .env; see keys below
  .gitignore
```
Activate: `.venv\Scripts\activate` (PowerShell) or call `.venv\Scripts\python.exe` directly.

## Installed (Python, .venv)
| Package | Version | Role per memo |
|---|---|---|
| anthropic | 1.14.0 | **core** – direct tool loop; exposes `browser_toolset_20260801`, `computer_toolset_20260801`, `advisor_20260301`, model ids `claude-{sonnet,opus,haiku}-5-5` |
| playwright | 1.64.0 | **core** – `connect_over_cdp` to the dedicated Chrome; Chromium 1248 build also downloaded for offline tests |
| aiosqlite | 0.22.1 | core – queue/answer store (stdlib sqlite 3.53 also present) |
| aiogram | 3.31.0 | core – Telegram bot (Phase 1) |
| google-api-python-client, google-auth-oauthlib | current | core – Gmail code helper (Phase 1) |
| browser-use | 0.13.11 | evaluation – alternative executor; has `integrations/anthropic` driver for Claude's browser toolset and `cdp_url` support |
| stagehand (py) | 4.2.1 | evaluation only – needs its own extension inside Chrome + `--enable-unsafe-extension-debugging`; poor fit per memo |
| langchain, langchain-anthropic, langgraph | 1.4.6 / 1.7.6 / 1.2.15 | evaluation only – memo rejects as orchestrator (interrupt() has no timeout) |
| openai | 3.31.0 | evaluation only – not chosen for the executor |
| pytest, pytest-asyncio, python-dotenv | current | dev |

Resolver notes: uv initially picked browser-use 0.11 + anthropic 0.125 + openai 2.26; explicit pins
(`anthropic>=1.14`, `browser-use==0.13.11`, `openai>=3`) resolve cleanly together. browser-use 0.13.11
pins pydantic==2.13.5 and pydantic-settings==2.15.0 exactly; expect friction if other libs move.

## Installed (Node, js/)
@browserbasehq/stagehand 4.2.1 (bundle confirms `cdpUrl` option and a bundled `dist/extension/`), openai 7.34.0, playwright 1.64.1, zod 4.6.7. Node 24.18, npm 11.16.

## Found on this machine
- Chrome 151.0.7922.172 at `C:\Program Files\Google\Chrome\Application\` (155 staged for update). Default profile exists at `%LOCALAPPDATA%\Google\Chrome\User Data\Default` — **do not automate it** (Chrome 136+ blocks CDP on the default profile).
- **Port 9222 is already taken**: the old Hermes pipeline's Chrome is running with
  `--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=...\hermesagent\internship_sweep\chrome-profile`.
  `/json/version` answers. Decide whether to reuse that profile (Simplify/Bitwarden may already be signed in) or make a fresh one on 9223 (the `.env.example` default).
- Old pipeline assets at `C:\Users\Surya\Documents\hermesagent\internship_sweep\` — import as **data**, not code (Phase 0):
  - `config/answer_bank.json`, `standing_answers.json` (11 keys), `surya_facts.md` (262 lines), `guardrails.md` (68 lines), `guardrail_patterns.json`, `trap_patterns.json`, `triage_rules.json` (15 rules; source = SimplifyJobs Summer2027 listings.json)
  - `data/queue.db`: tables `listings` (17,638 rows: 183 queued, 7 submitted, 309 rejected, 104 manual, 9 hold…), `answers` (202), `applied` ledger (397), `events` (1,219)
  - `records/` (83 per-listing files), `resumes/Surya_Thirukonda_Resume_SWE.pdf`
- Not installed: **Bitwarden CLI (`bw`)**, **Sysinternals Autologon**, Gmail OAuth client, Telegram bot token. Git 2.54 present; this folder is not yet a git repo.

## Next (not started)
Phase 0 per memo: git init (control repo), dedicated Chrome profile + port decision, SQLite schema,
import `queue.db`/`applied` into the ledger, seed answer store from the config files above, install `bw` CLI.
