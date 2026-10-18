---
name: surya-handoff
description: Surya watches the dedicated Chrome and sometimes fills a field himself; how to accept what he did and carry on
agents: [filler, chat]
triggers: [i already, i filled, i did it, done, leave it, as is, i entered, i selected, i clicked, continue, carry on, resume]
---
Surya can see the browser. When an answer to a question says he already filled it ("I already filled it out",
"done", "I clicked the boxes", "leave it as is"), or the broker returns `AS_IS` for a key:

1. `snapshot` the page.
2. For each field he set, `fill_batch` with source "as_is" and value null: the harness records what the page
   shows as his decision (user:manual). Never clear or re-set a value he put there.
3. Carry on with the rest of the page and `submit` as usual (shadow mode stops before the click).

In chat: "I filled DoorDash, continue" means `keep_as_is` on that job's open questions (so a waiting run picks
up at once), and `job_action run_now` if the job already parked. "Skip Lyft" means `job_action skip`.
"Pause" / "resume" are `worker` actions. Confirm in one line what you did, with the job's company and id.
