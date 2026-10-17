# Running goat in the cloud: next steps

Today goat runs on Surya's Windows PC: a dedicated Chrome on CDP port 9223, SQLite in `data/goat.db`,
the Bitwarden CLI unlocked in his terminal, a Gmail OAuth token in `secrets/`, and the Electron
dashboard. This plan moves that runner to a small Linux VM and adds phone notifications.

## The one risk to test first

Application portals treat datacenter IP addresses as more suspicious than a home connection. From a
cloud VM, expect more hCaptcha, reCAPTCHA and Cloudflare challenges on Workday, Greenhouse and Lever.
goat never solves those: it fills the form and parks the job as `ready_for_you`. So the cloud setup
must let Surya click a CAPTCHA in the cloud browser from his phone (step 5 below), and the shadow trial
(step 8) must measure the challenge rate before the PC runner is retired. If the rate is high, keep
the runner on the PC and use the cloud only for the dashboard and notifications. A residential proxy
would fix it but is a paid service: ask Surya first.

## Target setup

| Piece | Choice |
|---|---|
| Host | Ubuntu 24.04 VM, 2 vCPU, 4 GB RAM, 40 GB disk. Free option: Oracle Cloud Always Free (Ampere ARM). Paid options around $5-12 a month: Hetzner CX22, DigitalOcean, Lightsail. Ask before paying. |
| Packaging | One Docker image: Python 3.12 + requirements.lock, Playwright, Google Chrome stable (Chromium on ARM), Xvfb, x11vnc + noVNC, Node 20 + `@bitwarden/cli` |
| Process | `python -m goat run` as the container command, `restart: unless-stopped` |
| Storage | Volumes for `data/`, `records/`, `chrome-profile/`, `secrets/`, `resumes/` and the local-only `config/` files |
| Access | No public ports. Tailscale on the VM and on Surya's phone and PC: SSH, the dashboard (8787) and noVNC (6080) are reachable only on his tailnet |
| Notifications | Telegram bot (see the last section) |

## Code changes before the move

1. **Chrome on Linux.** `goat/config.py` defaults `CHROME_EXE` to the Windows path. Pick the default by
   platform (`/usr/bin/google-chrome` or `/usr/bin/chromium`), and have `goat/browser/chrome.py` start
   Chrome on the Xvfb display (`DISPLAY=:99`, window 1440x900, `--lang=en-US`), keeping the dedicated
   `--user-data-dir`.
2. **Entrypoint.** Add `scripts/entrypoint.sh`: start Xvfb, x11vnc and noVNC, then `python -m goat run`.
   `goat.cmd` and the Electron shell stay for Windows. The keep-awake call is already a no-op off Windows.
3. **Bitwarden unattended.** On the VM, log in with an API key (`BW_CLIENTID`, `BW_CLIENTSECRET` in
   `.env`) and unlock with `BW_PASSWORDFILE`, which `goat/creds/bitwarden.py` already supports. Add a
   re-unlock when a `bw` call reports a locked vault, so a long run survives session expiry.
4. **Gmail.** No code change. Copy `secrets/gmail_client.json` and `secrets/gmail_token.json`; the
   refresh token works headless because the OAuth app is published (External), so it does not expire
   after 7 days.
5. **CAPTCHA handoff.** When a job parks `ready_for_you`, include the noVNC link (the VM's Tailscale
   address, port 6080) in the notification. Surya opens it on his phone, clicks the challenge in the
   real cloud browser, and the job continues. The tab stays open; `tab_target_id` already reattaches it.
6. **Backups.** Nightly `sqlite3 data/goat.db ".backup ..."` to object storage. Neon Object Storage
   from the goat-dashboard project is free and already provisioned. Later, the goat-dashboard agent
   client can make Neon Postgres the source of truth instead of SQLite.
7. **Health.** A `/healthz` route on the local dashboard (CDP answering, last event age) for the Docker
   healthcheck. The watchdog already relaunches Chrome and retries portal errors.

## Deploy checklist

1. Create the VM, install Docker and Tailscale, and join it to the tailnet.
2. `git clone https://github.com/SuryaThirukonda/autoapply.git` on the VM.
3. Copy the local-only files listed in `config/LOCAL_FILES.md` over Tailscale with `scp`: the personal
   `config/` files, `resumes/`, `secrets/`, and a `.env` built from `.env.example`. Never through git:
   the repository is public.
4. Build the image and run the offline tests inside it: `python -m pytest -q` (100 pass on the PC).
5. Run `python -m goat check-api` to confirm the OpenAI key and the gpt-6-luna allowlist.
6. Stop the PC runner and copy `data/goat.db` to the VM. **Only one runner may run at a time**: each
   runner has its own ledger, so two would apply to the same listing twice.
7. Start the container in shadow mode (`GOAT_MODE=shadow`, `LIVE_PORTALS` empty), first with
   `LANES_ENABLED=no_account` (Greenhouse, Ashby, Lever), then add `account` (Workday, Oracle).
8. Shadow trial: let 10-20 jobs run and compare with tonight's PC results.

| Check | Pass when |
|---|---|
| Chrome under Xvfb | CDP on 9223 answers; screenshots in `records/` look like the PC's |
| Bitwarden | a Workday sign-in fills from the Boeing item with no prompt |
| Gmail | an emailed code is entered (Oracle onsemi) and a verification link opens (Workday) |
| Telegram | a question arrives with buttons, the reply releases the job |
| CAPTCHA rate | `ready_for_you` for challenges is not much above the PC's rate |
| Restart | killing the container requeues the in-flight job, and it resumes |
| CAPTCHA handoff | the noVNC link from a notification opens the cloud browser on the phone |

9. Once the trial passes, Surya reviews drafts as before. Portals go live only when he clears them.

## Notifications: Telegram or Gmail?

**Telegram is easier, and it is already built.** `goat/human/telegram_bot.py` sends each question with
the page screenshot and one-tap option buttons plus a "Hold it" button. It also takes free-text
replies, sends a ping when a job needs Surya, and posts a daily digest at 18:00. It uses long polling,
so it needs only outbound internet: it works the same on the PC and on a cloud VM. It is free, and
pushes reach the phone instantly. What is missing is configuration:

1. In Telegram, message **@BotFather**, send `/newbot`, pick a name, and copy the token.
2. Send any message to the new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy `message.chat.id`.
3. Put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_CHAT_ID` in `.env` (the bot answers only that chat).
4. Restart goat and run `python -m goat digest`: the digest should arrive on the phone.

**Gmail would be new work.** goat's Gmail access is read-only (`gmail.readonly`), used for codes and
verification links. Sending notifications needs the `gmail.send` scope, a fresh OAuth consent, code to
compose messages, and code to read replies out of email threads to answer questions. Notifications
would also land among application emails and arrive only as fast as the mail app syncs. Gmail is
reasonable later as a backup channel for the daily digest, not as the main one.

The goat-dashboard web app (Next.js on Vercel, Neon) plans web push from an installable PWA. That can
replace or join Telegram once its phase 3 ships.
