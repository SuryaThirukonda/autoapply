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

Dropdowns: "Select One" buttons list every option and CLOSE when typed into; `choose` handles them (it reads the
list before typing). A prompt widget shows your typed search text whether or not it committed: only the selected
value chip counts, and `choose` reports "did not commit" honestly; then try `choose` once more with the exact
option text, else the `computer` tool (click the option's radio).

Dates: month / day / year are three 1 px sections; use `set_date` with a full date (05/18/2027). It tabs out and
re-reads; "Invalid Date" means it retries with arrow keys. Work-experience "From" dates on GE Vernova and Cigna
needed this.

School: pickers spell it "Texas A & M University" (the `education.school_variants` chain has every spelling).
How did you hear: an ordered chain (LinkedIn, Job Board, Agency / Search Firm, Other); the second level under
Job Board uses `preferences.job_board_second_level` (company website first).

Pages: after "My Information", "My Experience", "Application Questions", "Voluntary Disclosures",
"Self Identify" comes "Review": read it back, fix only what is wrong, then `submit`. A Submit button that appears
on a page with a visible password box is the account page's button: never press it.
Errors: "Something went wrong / Error Code VPS" is a portal failure: park as `portal_error`; the watchdog retries
in a fresh tab after two hours.
