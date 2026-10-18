---
name: greenhouse
description: Greenhouse job boards (job-boards.greenhouse.io and embedded forms): react-select pickers, 255-character short answers, the post-submit security code, wrapped forms
portals: [greenhouse]
agents: [filler, chat]
triggers: [greenhouse, react-select, security code]
---
Open the embedded form, never the wrapper: `?gh_jid=` pages (company sites, careerpuck) and
`/<board>/jobs/<id>` pages redirect to `job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>`; the
worker does this hop itself when it sees a Greenhouse iframe.

Pickers are react-select: `choose` only, never type-and-Enter. Lists for school, graduation date, honors and
disability load from the network and can take two seconds to appear; `choose` waits. The search input is a few
pixels wide; the harness clicks the control box. Helper text under a filled picker ("Please choose the closest
conversion") is a hint, not an error.

Country before phone: picking the country clears a typed phone number; prefill already orders this, and re-types
anything a later step cleared.

Résumé: the file input is hidden; `upload_resume` targets the slot named "resume" (label may read "Attach").
Greenhouse shows the file name only after it has processed the upload; the harness waits for it. Never upload
twice: the second upload lands in the cover-letter slot.

Short answers are capped at 255 characters: the outline marks each box "max N chars"; count before typing.
Location (City) is an autocomplete: `choose` with `identity.location_city` (Sugar Land, Texas, United States).
Multi-select checkbox groups (offices, organizations): tick every location when the stored answer says
"All offered locations"; for team / organization groups pick the most backend / platform-flavored ones under
`preferences.team_or_product`.

After Submit Greenhouse sometimes emails an 8-character security code: call `enter_email_code` with the code box
ref, then `submit` again. The "protected by reCAPTCHA" badge is not a challenge. A real reCAPTCHA checkbox is:
fill everything, then park `ready_for_you`.
