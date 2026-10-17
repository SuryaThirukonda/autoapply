# goat — design notes (implementation of "Swap in OpenAI, keep Playwright, skip Stagehand")

One asyncio process, one SQLite file (`data/goat.db`, WAL, one writer), one Telegram bot.
The model drives the browser through a direct **Responses API** loop; everything consequential
(passwords, emailed codes, the final submit, the 15-minute timer, the ledger) is deterministic
Python the model can call but cannot bypass.

## Package layout

```
goat/
  config.py        env + paths + models + budgets (read once at startup, logged with a hash)
  db.py            schema, connection (WAL), small transaction helpers
  states.py        the ten job states and the allowed transitions
  answers.py       versioned answer store: exact keys, aliases, seeds, write-back
  ledger.py        never-apply-twice: canonical keys (ported from Hermes), applications table
  triage.py        deterministic listing triage (ported from Hermes sweeplib.classify)
  scraper.py       SimplifyJobs listings.json diff -> listings -> jobs; liveness checks
  records.py       per-job folder: decisions record, screenshots, redaction
  budget.py        per-job budgets (steps, wall clock, failed submits), BudgetExceeded
  gate.py          deterministic submit gate: returns named results, never "unclear"
  worker.py        claim -> lane -> adapter prefill -> agent loop -> gate -> submit -> ledger
  supervisor.py    asyncio entry point: scraper, worker, bot, mail, watchdog coroutines
  cli.py           python -m goat <command>
  browser/
    chrome.py      launch / health-check the dedicated Chrome (CDP on 127.0.0.1)
    executor.py    Playwright over CDP: pages, screenshots, guarded clicks, typing, upload
    snapshot.py    pruned accessibility outline with refs (e1..eN) + error summary first
    errors.py      page error scan + positive submit signal (JS ported from Hermes, proven live)
    fill.py        fill_batch / choose / upload_resume with per-field read-back
    captcha.py     challenge detection; emailed-code vs CAPTCHA classification
    submit.py      the only path to submission: scan -> click -> wait for a positive signal
  adapters/        per-ATS field maps and widget drivers (greenhouse, ashby, lever, workday, ...)
  agent/
    loop.py        Responses loop: gpt-6.1-sol low, retry at medium via configuration_update
    tools.py       function tool schemas + dispatcher (the table in the memo)
    policy.py      host allowlist, pre-approval, pending_safety_checks policy, click target checks
    advisor.py     gpt-6-astra failure brief (max 3 per application)
    classifier.py  gpt-6-luna (effort none) structured outputs: question->key, page type
    prompts.py     cached prefix: instructions, pre-approval, never-invent rule
  human/
    questions.py   ask_user / await_answers (15 min from first ask), Parked
    telegram_bot.py aiogram bot: owner-only, forced replies, buttons, /remember, save+release
  mail/gmail.py    Gmail API read-only helper: codes and verification links, portal-scoped
  creds/bitwarden.py bw CLI helper: generate, store, fill; never returns a password
```

## Ten states

`new -> queued -> filling -> {awaiting_answer, awaiting_code, gated, ready_for_you, hold}`
`gated -> submitting -> submitted -> confirmed`. Terminal-ish: `triaged_out`, `dead`.
A `submitting` marker is committed before the click; after a crash anything in `submitting`
is reconciled (confirmation email or portal "already applied") before any retry.

## Answer store

`answers(key, scope, scope_value, answer, answer_type, source, source_ref, created_at,
superseded_by)`. Exact key first; then `question_aliases` (normalized text or Hermes-style
substring sets) ; then gpt-6-luna mapping above `CLASSIFIER_MIN_CONFIDENCE`; else `ask_user`.
Seeds: standing_answers.json (flattened to `section.key`), answer_bank.json (entries become
`bank.<id>` keys with their match lists as aliases), delegation.yaml self-ID and consents.
The bot's reply handler upserts the answer and runs
`UPDATE jobs SET status='queued' WHERE status='hold' AND blocked_on_question_key=?`
in the same transaction, then sets an asyncio.Event so a waiting worker wakes.

## Agent loop (per form page)

```
items = cached_prefix(job) + page_history
resp = client.responses.create(model=EXECUTOR, reasoning={"effort": "low"},
        tools=[{"type":"computer"}, *FUNCTION_TOOLS], input=items, store=False,
        parallel_tool_calls=False, safety_identifier=hash, include=["reasoning.encrypted_content"])
```
`computer_call` -> policy.review(pending_safety_checks) -> per-action target check -> screenshot
back (`detail: original`, 1440x900, DPR 1). `function_call` -> tools.dispatch. A plain-text
message asking to submit is answered from policy; other questions route to `ask_user`.
One retry per failed step at `medium` through a `configuration_update` item.

## Guarantees enforced in code (not prompts)

- No click lands on a submit control outside `submit()`, nor inside a CAPTCHA frame.
- `submit()` refuses while the page scans dirty; returns submitted | blocked | failed.
- Passwords and emailed codes never enter model context, logs, snapshots or records.
- Navigation only to hosts derived from the listing (plus known ATS hosts).
- The agent has no shell, no file write outside `records/` and `proposals/`.
- Every value in the decisions record has a source; attestations only from standing answers.
