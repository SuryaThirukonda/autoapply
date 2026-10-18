---
name: ashby
description: Ashby (jobs.ashbyhq.com): single page, virtualized fields, hidden radios and toggle buttons, autofill-from-resume side effects
portals: [ashby]
agents: [filler]
---
One page, often short. Fields are virtualized: after scrolling, take a fresh `snapshot`. Yes/No questions are
toggle buttons or hidden radios: `fill_batch` with the answer_key presses the option whose own label agrees with
the stored answer (the harness refuses the other one).

Uploading the résumé can trigger "Autofill from resume", which rewrites name, email, phone and location: read
the form back afterwards and re-set anything that differs from the stored answers (the gate rejects values
without a sourced decision from this attempt, so re-set them even when they look right).

Location is `identity.location_city` ("Sugar Land, TX"). Cohort / start-date questions resolve from the
availability window (05/18/2027 to 08/14/2027): pick the cohort that fits inside it.
Harvey caps applications at 4 per 30 days; the worker holds a job that would break the cap and requeues it when
the window opens.
