---
name: workday
description: Workday tenants (myworkdayjobs.com): one universal login, Select One dropdowns, 3-part dates, the Submit button that sits where Save and Continue was
portals: [workday]
agents: [filler, chat]
triggers: [workday, sign in, create account, verification]
---
Account first: every Workday tenant takes the same login. On a Sign In / Create Account page call `credentials`
(mode `login_universal`, then `create` if the account does not exist), then `click` Sign In / Create Account
(never `submit`). If sign-in fails or the page says to verify the email, call `open_verification_link`, then
sign in again. Verification emails come from `<tenant>@otp.workday.com`; the harness checks the tenant.

Older tenants hold Surya's account under a previous password: Sign In says "You may have entered the wrong
email address or password or your account might be locked" and Create Account bounces straight back to Sign
In. Do not loop. Click "Forgot your password?", fill the email (`identity.email`), click the send button, call
`open_password_reset_link` (it waits for the tenant's reset email and opens it), `snapshot`, then `credentials`
mode `set_password` on the New Password and Verify New Password boxes (email_ref ''), `click` the save button,
and sign in with `credentials` login_universal. The new password is his one application password, so the next
tenant in this family signs in at once.

Dropdowns: "Select One" buttons list every option and CLOSE when typed into; `choose` handles them (it reads the
list before typing). A prompt widget shows your typed search text whether or not it committed: only the selected
value chip counts, and `choose` reports "did not commit" honestly; then try `choose` once more with the exact
option text, else the `computer` tool (click the option's radio).

Dates: month / day / year are three 1 px sections; use `set_date` with a full date (05/18/2027). It tabs out and
re-reads; "Invalid Date" means it retries with arrow keys. Work-experience "From" dates on GE Vernova and Cigna
needed this.

School: pickers spell it "Texas A & M University" (the `education.school_variants` chain has every spelling).
How did you hear: an ordered chain (LinkedIn, Job Board, Agency / Search Firm, Other). Some tenants nest it
(Nissan): LinkedIn sits under "Digital/Social Media", Handshake / Indeed under "Job Board"; `choose` drills into a
category and types each preference into the prompt's search box. The second level under Job Board uses
`preferences.job_board_second_level` (company website, then Handshake, LinkedIn, Indeed). Ask Surya only when
none of those exist anywhere in the tree.

My Experience: the harness fills Work Experience and Websites itself ("adapter:sections" when the page opens; `fill_sections`
whenever you call it): every `experience.work.N` entry in order (Add / Add Another, title, company, location, dates, the
"I currently work here" tick, the full résumé bullets, trimmed only to a box's character cap) and every `experience.websites`
URL, skipping what is already there. Read its report, `snapshot`, and finish only the items it lists as failed; never add an
entry by hand first. Education: school via `education.school`; on tenants whose note says "press Enter to confirm your entry" (Nissan)
`choose` types the name and presses Enter itself when no list match appears. Degree picker, Field of Study (a search
prompt: `choose` with education.major), GPA. Skills and Websites: `fill_sections` adds them too (Skills in the portal's wording: 'Structured Query Language (SQL)', 'Java (Programming Language)'; a skill it reports as not offered stays out). Never type into the Skills box yourself: a keystroke in its chip list has selected whole result lists. The résumé upload sits on the same page. A resumed
application opens at its last step: `go_to_step("My Experience")` jumps there through the rail (the outline does not
list the rail), fill, then Save and Continue forward through every step to Review. The gate refuses a Review that still
shows "Work Experience: No Response".

Pages: after "My Information", "My Experience", "Application Questions", "Voluntary Disclosures",
"Self Identify" comes "Review": read it back, fix only what is wrong, then `submit`. A Submit button that appears
on a page with a visible password box is the account page's button: never press it.
Errors: "Something went wrong / Error Code VPS" is a portal failure: park as `portal_error`; the watchdog retries
in a fresh tab after two hours.
