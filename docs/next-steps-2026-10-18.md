# goat: next steps toward reliable, autonomous autofill

Drafted 2026-10-18 from (a) the live shadow runs of that evening, (b) the research memo
[research-application-agents-2026-10-18.md](research-application-agents-2026-10-18.md), and (c) the open
items in the queue. Each step names what it fixes, the evidence, and a rough size (S = an evening,
M = a few days, L = a week or more).

## What the live runs showed (2026-10-18, shadow mode, 9 runs)

| Form | Result | What it taught |
|---|---|---|
| Harvey (Ashby) | shadow draft, 67 s | Cohort dates derived from his availability window; team pick from `preferences.team_or_product`. Clean. |
| Niantic Spatial (Ashby) | shadow draft, 23 s | Prefill plus one lookup; the gate caught two prefilled toggles with no sourced decision and the model re-set them. Clean. |
| ICF International (Workday) | hold: credentials | The "submit it yourself" policy worked (it clicked Apply Manually and went on). Blocked only because the Bitwarden CLI is logged out on cadoganpc. |
| GCM Grosvenor (Greenhouse) | hold, wrong reason | Three defects: the eligibility bounce called a Dec 2028–Spring 2029 graduation window "satisfied"; "relationships with Grosvenor" (plural) missed the default-No family and was asked; the model parked two seconds after asking. All three fixed the same night. |
| DoorDash (Greenhouse) | hold, wrong reason | Two react-select pickers whose option lists load from the network were declared "did not open" after 0.6 s; the organization-preference multi-select was asked instead of chosen; parked while the question was pending. Fixed: pickers poll up to 2.4 s; the prompt now picks backend/platform-flavored orgs itself; park waits. |
| Verkada (Greenhouse) | hold, wrong reason | Helper text "Please choose the closest conversion" under a filled GPA picker was read as a validation error, so the gate blocked a complete form. Fixed: unmarked please-choose text beside a filled control is a hint. |
| Résumé on Greenhouse job-boards | prefill reported "page does not show Surya_Thirukonda_Resume_SWE.pdf" | The chip appears only after Greenhouse processes the upload; the check now polls up to 6 s. |

Pattern: six of the seven failures were the harness being too sure too early (a 0.6 s wait, a regex on
"2028", an exact-match regex, a one-shot DOM read), and the model then rationalised a park. None were
model reasoning failures. That points the roadmap at verification and patience in the harness, not at
more prompt text.

## Ranked next steps

1. **Read-back diff before the gate (S).** After the model says a page is done, read every field's DOM
   value and compare it with the intended answer from the decision log; refuse Submit on any mismatch
   and hand the model the exact list. Today the gate checks "sourced decision exists" and page errors,
   not value == intent. This is the single check that would have caught the Verkada and Workday
   picker-commit cases before the model saw them. Evidence: protoAgent #4032, Skyvern.
2. **Three-tier answer confidence (M).** Tag every stored answer `confirmed` (Surya said it),
   `inferred` (derived by the resolver from the profile) or `sensitive` (DOB, pay, self-ID). Act
   silently on confirmed; collect inferred ones into the finish message for a one-tap "all good /
   fix #3"; re-confirm sensitive ones the first time each portal asks. Removes the top complaint about
   every commercial auto-applier (wrong screening answers) without more interruptions.
3. **One batched question card per application (M).** `ask_user` already collects every unknown on a
   page; extend it to hold questions until the page is otherwise complete and send one Telegram
   message per application with proposed answers and the choices listed, instead of one message per
   question. Pairs with the free-reply interpreter shipped today. Mirrors Simplify's review section.
4. **ATS fixture suite with a per-adapter CI gate (M).** Save the HTML of each portal's apply page
   (Greenhouse job-boards, Ashby, Lever, Workday, Oracle) with secrets scrubbed and replay through
   `page.route` / `routeFromHAR`; one test per adapter per failure mode (slow picker, helper text,
   session-timeout dialog, cleared phone, Invalid Date). Portal markup changes then fail a test, not an
   application. The seven edge-case tests written this week are the seed.
5. **Cheap binary judge on completed shadow runs (S).** After each shadow draft, one gpt-6-luna call
   over the decision log plus final screenshot: "every required field holds the intended value?" with a
   bucketed failure reason. Store the verdict with the job; show it on the website; use the buckets to
   pick the next fix. Browser Use reports 85–87% human agreement for this shape of judge.
6. **Greenhouse question-schema prefetch (S).** Greenhouse job boards expose each job's questions as
   JSON (`/embed/job_app?for=<board>&token=<id>` or the public boards API). Resolve every question
   offline before the tab opens, then the page visit is fill-and-verify. Cuts model steps on the
   largest portal in the queue (about 60% of listings).
7. **Per-site action replay with LLM fallback (L).** Record a successful trajectory per
   portal + tenant as a Playwright script keyed on a form fingerprint; replay without the model;
   on any failed step fall back to the agent and re-record. Skyvern code-caching and Stagehand's cache
   show 5–10x cost and latency drops. Worth it once the fixture suite exists, not before.
8. **Headed hand-off for CAPTCHA / sign-in / 2FA (S).** On `ready_for_you` or a credentials hold,
   bring the dedicated Chrome window forward on cadoganpc and send a Telegram message with the job
   link; resume the run when the page changes. The credentials hold today (Bitwarden logged out) would
   have become a one-line Telegram nudge instead of a silent hold.
9. **Heartbeat summary turn (S).** Every 30–60 min the supervisor posts to Telegram only if something
   needs Surya (held jobs grouped by reason, open questions, stale queue); otherwise nothing. Replaces
   the per-event alerts the digest half-covers. OpenClaw's `NO_REPLY` pattern.
10. **Curated MEMORY.md + daily run notes (M).** Move the standing decisions now scattered across
    `profile.md`, `questionnaire.yaml` and prompt text into one curated file injected per run, with a
    daily notes file the worker appends to (what each run learned, which picker was slow where); a
    nightly sweep promotes durable items. Prevents re-asking settled questions and gives the fixtures
    in step 4 their examples.
11. **Sensitive-value placeholders (S).** Phone, address, DOB and pay never appear in the model
    prompt; the model names the field and the harness types the value. Browser Use's `sensitive_data`
    design; goat already does this for passwords and codes.
12. **Nightly canary set (M).** Re-run 10–20 shadow applications across adapters each night on
    fixtures or live boards that allow it, with success rates and bootstrap intervals on the website's
    Stats page. Single runs hide variance; today's second pass is a manual version of this.
13. **Untrusted-content guard (M).** Job descriptions and portal copy are data; add a small
    classifier that flags instruction-like text on a page before the model acts, and an allowed-domains
    list per run (the ATS host and its redirects). The dominant failure class in the OpenClaw and
    Claude-for-Chrome security write-ups.

## Order of work

Steps 1, 5 and 6 first (each an evening, each cuts a whole failure class seen tonight), then 2 and 3
together (they share the answer-store change and the finish message), then 4 as the safety net before
8–9 widen autonomy, then 7 once fixtures exist. 10–13 are hygiene that can ride along.

## Blockers outside the code

- Bitwarden CLI is logged out on cadoganpc (`bw status` = unauthenticated): every Workday / Oracle
  account-lane job holds at sign-in until `bw login` is run there. Ten account-lane jobs are held with
  that reason and requeue with one click once it is back.
- C++ years of experience is the one questionnaire item still unanswered.
