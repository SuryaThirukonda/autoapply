# Questionnaire: how goat answers recurring questions

goat only types what its answer store holds, plus free-response text it drafts from your profile.
Your decisions live in [config/questionnaire.yaml](../config/questionnaire.yaml). After any edit, run:

```bash
.venv\Scripts\python.exe -m goat seed-questionnaire
```

You can also change or forget any remembered answer in the dashboard's Answers tab. Every change is a
new version, and nothing is deleted.

## Where answers come from

1. **Your questionnaire and chat answers.** These are the 2026-10-15 answers, merged with the answer bank
   built from Claude chat. Where the two disagreed, your chat answer won.
2. **Your profile**, in [config/profile.md](../config/profile.md). It holds everything known about you.
   The agent drafts every free-response answer from it and never asks you to write one.
3. **Questions you answered from the dashboard pop-up.** Each one is remembered and reused.
4. **Settled from your profile.** These are yes/no questions about employers, industries, affiliations
   and relationships, such as "employed by a mortgage servicer?". They default to No unless the profile
   plainly says Yes. Each is saved and shown under "Settled from profile" in the Answers tab.

## Rules the agent follows without asking

| Situation | What it does |
|---|---|
| Certification, attestation, privacy, terms, data-processing boxes | Ticks them for you |
| Background-check, drug-screen and anti-nepotism acknowledgements | Ticks them for you |
| SMS about the application | Opts in |
| Marketing email, newsletters, job alerts, talent community | Leaves them unticked |
| Electronic signature | Types "Surya Thirukonda", with today's date beside it |
| Relocation at your own expense | Yes when the posting lists pay. Otherwise No, and relocation assistance required |
| Applied here before | Checks the application ledger |
| Middle name, apartment, emergency contact, optional "anything else" | Leaves them empty |
| References | Types "Available upon request" in a text box, and parks if a form wants names |
| Know anyone at / referred by / met at an event | No, except Southwest Airlines, LPL Financial and Lockheed Martin, where it asks you |

It still never solves a CAPTCHA, never types a password, never enters an SSN or government ID number,
and always answers AI-use disclosures truthfully.

## Still open

Every item with `answer: null` in [config/questionnaire.yaml](../config/questionnaire.yaml) is open. Most are
in the `questions_for_surya` section and the `skills.*` items, added 2026-10-15 from the questions goat and
Hermes met plus common application-form fields (work-history contacts, supervisors, pay history, reasons for
leaving, first-generation status, credit hours, current courses, skill years and ratings, physical
requirements). The rest are compliance and availability items listed earlier (discipline history, debarment,
military support, date of birth, passport, semester hours, grad-school plans, Agile, financial services).
If a form requires one, the agent asks through the dashboard pop-up and remembers the answer.

Settled 2026-10-15: U.S. citizenship only; no postgraduate degree right after the internship.
