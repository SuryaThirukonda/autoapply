---
name: questions
description: When to ask Surya, how to phrase a question, and what never to ask
agents: [filler, chat]
triggers: [question, ask]
---
Ask only for a FACT or a PREFERENCE he has not stated anywhere: `lookup_answer` first (it reads the whole
knowledge base by meaning and opens dropdowns to read every choice), then `ask_user` with every unknown on the
page at once. Keep filling; call `await_answers` only when nothing else is left. Never park within the first
minutes of asking: the clock is 15 minutes, and the harness bounces such a park.

Never ask him to: write prose (draft it from the PROFILE), pick a team / organization / track (choose the most
backend, platform or infrastructure-flavored option under `preferences.team_or_product`), answer an
employer / industry / affiliation / relationship yes-no (default No from the profile), choose a location
(all offered, else Dallas / Houston / Austin, else the largest city), consent or sign (he consents on every form).

Questions reach him on Telegram as one message with the choices listed; he answers in his own words and the
broker maps the reply onto a choice. "hold" holds the job. A reply like "I already filled it out" comes back
as `AS_IS`: see surya-handoff.
