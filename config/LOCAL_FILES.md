# Local-only config

This repository is public, so the files that describe the applicant stay on the machine that runs the
agent and are listed in `.gitignore`. A fresh clone needs them copied in before `goat` can fill a form,
and several tests seed from them.

| File | What it holds |
|---|---|
| `profile.md` | everything the agent may use when drafting free-response answers |
| `questionnaire.yaml` | standing answers to recurring questions (`python -m goat seed-questionnaire`) |
| `standing_answers.json` | contact details, education, work authorization |
| `answer_bank.json` | the older answer bank with match phrases |
| `delegation.yaml` | self-identification choices, résumé file names, hold rules |
| `surya_facts.md`, `guardrails.md` | facts and the never-claim list behind the accuracy guardrails |
| `../resumes/` | résumé PDF and text, transcript |
| `../.env`, `../secrets/` | API keys, Bitwarden session, Gmail OAuth client and token |

Copy them over a private channel (scp, a password manager attachment, an encrypted archive), never
through git. The committed files in this folder (`guardrail_patterns.json`, `trap_patterns.json`,
`triage_rules.json`, `writing_rules.md`) are generic.
