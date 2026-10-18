---
name: oracle
description: Oracle Cloud recruiting (oraclecloud.com): email-code sign-in titled "Confirm Your Identity", the "Are You Still With Us?" timeout dialog, :443 hosts
portals: [oracle]
agents: [filler]
---
Sign-in is by emailed code: the message is titled "Confirm Your Identity"; `enter_email_code` fetches and types
it. Hosts sometimes carry an explicit `:443`; the harness normalises it.

A session-timeout dialog "Are You Still With Us?" appears on long forms: the snapshot presses Continue before
reading the page (never Sign Out or Cancel). A required dual-citizenship picker with no fitting option (Vertiv)
is a question for Surya, not a guess; the stored answer is "No, U.S. citizen only".
