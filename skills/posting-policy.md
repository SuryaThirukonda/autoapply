---
name: posting-policy
description: Which postings Surya pursues and which are holds (his rules of 2026-10-15 and 2026-10-18)
agents: [filler, chat]
triggers: [eligible, eligibility, local, relocat, hardware, clearance, unpaid, graduation, master]
---
Apply (a posting that STATES a requirement is not a restriction):
- U.S. work authorization, no sponsorship, Bachelor's in progress, any GPA minimum, 18+, graduation windows
  that include May 2028, onsite / hybrid / remote anywhere in the U.S., eligibility or willingness to obtain a
  clearance, a driver's license or "own transportation", a passport, housing elsewhere.
- "Must be submitted by the applicant personally": fill it and submit; he authorized the run and reviews drafts.
- Local-candidates / commuting-distance / no-relocation-help: fine when the posting lists pay (any hourly or
  annual figure); he relocates for a paid summer at his own expense.
- EE / CE "or related discipline" when the work is software (firmware, embedded software, data, tooling).

Hold with reason `eligibility: ...`:
- Graduation in 2027 or earlier, or a window that starts after May 2028 (Dec 2028 - Spring 2029, class of 2029).
- Master's / PhD programs; an ACTIVE clearance on day one; returning interns only; fall / spring co-ops.
- Unpaid AND local-only with no relocation help. Straight hardware roles (circuit / PCB / analog, RTL, FPGA, ASIC).
- Quantitative trading roles. Companies on the blocked list (Netic, Cybernetic Labs).

Park `needs_you: ...` (Surya is told): a required recorded video, portfolio or essay FILE upload; a required
supervisor name or emergency contact; references with names and contacts.
Caps: Waymo 5 per 60 days, Harvey 4 per 30 days, Scale AI 90 days per role (config/triage_rules.json).
