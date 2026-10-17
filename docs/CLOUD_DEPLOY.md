# Running goat in the cloud

Today goat runs on Surya's Windows PC: a dedicated Chrome on CDP port 9223, SQLite in `data/goat.db`,
the Bitwarden CLI unlocked in his terminal, a Gmail OAuth token in `secrets/`, and the Electron
dashboard. This plan moves that runner to a small Linux VM in Docker and adds phone notifications. The
container pieces are in the repository (see "What is built"); the VM itself is not created yet.

## The one risk to test first

Application portals treat datacenter IP addresses as more suspicious than a home connection. From a
cloud VM, expect more hCaptcha, reCAPTCHA and Cloudflare challenges on Workday, Greenhouse and Lever.
goat never solves those: it fills the form and parks the job as `ready_for_you`. So the cloud setup
lets Surya click a CAPTCHA in the cloud browser from his phone (noVNC, below), and the shadow trial
must measure the challenge rate before the PC runner is retired. If the rate is high, keep the runner
on the PC and use the cloud only for the dashboard and notifications. A residential proxy would fix it
but is a paid service: ask Surya first.

## Target setup

| Piece | Choice |
|---|---|
| Host | Ubuntu 24.04 VM, 2 vCPU, 4 GB RAM, 40 GB disk. AWS is the chosen provider; instance type and price are agreed with Surya before anything is created. |
| Packaging | One Docker image (`Dockerfile`): Python 3.12 + `requirements.lock`, Google Chrome stable on x86-64 (Debian's Chromium on ARM, which Google does not build for), Xvfb, x11vnc + noVNC, Node 22 + `@bitwarden/cli` (the CLI needs Node 22 or newer) |
| Process | `scripts/entrypoint.sh` starts the display stack, then `python -m goat run`; `restart: unless-stopped` |
| Storage | Bind mounts for `data/`, `records/`, `proposals/`, `chrome-profile/`, `secrets/`, `resumes/`, `config/` and `.env` (`docker-compose.yml`). The image holds code only (`.dockerignore`). |
| Access | No public ports. The dashboard (8787) and noVNC (6080) are published on the VM's loopback only; `tailscale serve` carries them to Surya's phone and PC on his tailnet. SSH too. |
| Notifications | Telegram bot (see the last section) |

## What is built

| Item | Where |
|---|---|
| Chrome on Linux. `CHROME_EXE` defaults by platform: `/usr/bin/google-chrome`, else `/usr/bin/chromium`. On Linux Chrome draws on the Xvfb display `CHROME_DISPLAY` (`:99`), window 1440x900, `--lang=en-US`, with the dedicated `--user-data-dir`. In Docker `CHROME_NO_SANDBOX=true` adds `--no-sandbox`: Docker's seccomp profile blocks the namespaces Chrome's own sandbox needs, so the container is the boundary. The Windows launch is unchanged. | `goat/config.py`, `goat/browser/chrome.py` |
| Entrypoint: Xvfb, x11vnc (container loopback only), noVNC via websockify on 6080, then the command. If any piece exits, the container exits and restarts as a whole; in-flight jobs are requeued on start. | `scripts/entrypoint.sh` |
| Image and compose, plus a `goat-test` service that runs the offline tests in the image, reads `config/`, `resumes/` and `.env` read-only, and writes only to `/tmp` | `Dockerfile`, `docker-compose.yml`, `.dockerignore` |
| Bitwarden unattended. When the CLI is logged out and `BW_CLIENTID`/`BW_CLIENTSECRET` are set, goat runs `bw login --apikey` (optionally `bw config server $BW_SERVER` first), then unlocks with `BW_PASSWORDFILE`. Any `bw` call that reports a locked vault unlocks again and is retried once. The key travels only in the subprocess environment, never argv or the log. CLI state persists in `secrets/bitwarden-cli/`, so the login happens once. | `goat/creds/bitwarden.py` |
| Gmail: no code change. The OAuth app is published (External), so the refresh token works headless and does not expire after 7 days. | — |
| CAPTCHA handoff. `NOVNC_URL` goes into every `ready_for_you` Telegram ping (placed before the tab URL so the 1024-character caption limit never cuts it) and onto the dashboard (a banner when jobs are one click from done, and an "Open cloud browser" button on the job). The tab stays open; `tab_target_id` reattaches it. | `goat/worker.py`, `goat/dashboard.py`, `goat/dashboard.html` |
| Health. `GET /healthz` returns 200 when CDP answers and `goat.db` is readable, else 503, with the newest event's age. `HEALTH_MAX_EVENT_AGE_S` > 0 also fails a run that has gone quiet. The image's `HEALTHCHECK` calls it. The watchdog still relaunches Chrome and retries portal errors. | `goat/dashboard.py`, `Dockerfile` |
| Nightly backup: SQLite's online backup API (what `.backup` uses, safe while goat writes), `PRAGMA quick_check`, gzip into `data/backups/goat-YYYYmmdd-HHMMSS.db.gz`, keep 14. Optional `--upload-cmd` / `BACKUP_UPLOAD_CMD` for a tool available where it runs. | `scripts/backup_db.py` |

Later, the goat-dashboard agent client can make Neon Postgres the source of truth instead of SQLite.

## Deploy checklist

Commands marked **VM** run in an SSH session on the VM, in `~/autoapply`. Commands marked **PC** run
in PowerShell in `C:\Users\Surya\Documents\bub`. `goat-vm` is the VM's Tailscale name.

1. **VM: install Docker and Tailscale, and join the tailnet.**

   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
   sudo usermod -aG docker $USER
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --ssh --hostname goat-vm
   ```

   Log out and back in so the `docker` group applies. From here on, SSH over Tailscale: `ssh goat-vm`.

2. **VM: clone the repository.**

   ```bash
   git clone https://github.com/SuryaThirukonda/autoapply.git ~/autoapply && cd ~/autoapply
   mkdir -p data records proposals chrome-profile secrets resumes
   ```

3. **PC: copy the local-only files over Tailscale.** Never through git: the repository is public.

   ```powershell
   scp config/profile.md config/questionnaire.yaml config/standing_answers.json config/answer_bank.json config/delegation.yaml config/surya_facts.md config/guardrails.md goat-vm:autoapply/config/
   scp -r resumes goat-vm:autoapply/
   scp secrets/gmail_client.json secrets/gmail_token.json goat-vm:autoapply/secrets/
   scp .env goat-vm:autoapply/.env
   ```

   `docker-compose.yml` overrides the Windows paths and `BW_SESSION` that the PC's `.env` carries.
   Do not copy `chrome-profile/`: Windows encrypts its cookies for that PC, so the VM starts a fresh
   profile.

4. **VM: finish `.env` and the Bitwarden password file.** Add the cloud keys to `.env`
   (see `.env.example`): `BW_CLIENTID` and `BW_CLIENTSECRET` (web vault: Settings > Security > Keys >
   View API key), `NOVNC_URL=http://goat-vm:6080/vnc.html?autoconnect=1&resize=scale`, and optionally
   `VNC_PASSWORD`. Then store the master password without it touching the shell history:

   ```bash
   ( umask 077; read -rsp 'Bitwarden master password: ' pw && printf '%s\n' "$pw" > secrets/bw_password )
   chmod 600 .env secrets/*
   ```

   If `id -u` does not print 1000, add `GOAT_UID=<that number>` and `GOAT_GID=<id -g>` to `.env`, so
   the container user can write the bind mounts.

5. **VM: build the image and run the offline tests inside it.**

   ```bash
   docker compose build
   docker compose run --rm goat-test
   ```

   Every test passes on the PC (`.venv/Scripts/python.exe -m pytest -q`); the same count must pass here.

6. **VM: check the OpenAI key and the gpt-6-luna allowlist.**

   ```bash
   docker compose run --rm --no-deps --entrypoint python goat -m goat check-api
   ```

7. **Move the database.** **Only one runner may run at a time**: each runner has its own ledger, so
   two would apply to the same listing twice. Stop the PC runner first (quit the goat window or
   `goat.cmd`), then copy a consistent snapshot (a bare copy of `goat.db` would miss rows still in
   the WAL file):

   ```powershell
   .venv\Scripts\python.exe scripts\backup_db.py --dest data\handoff --keep 1
   scp data\handoff\<the file it printed> goat-vm:autoapply/data/handoff.db.gz
   ```

   ```bash
   gunzip -c data/handoff.db.gz > data/goat.db && rm data/handoff.db.gz
   ```

8. **VM: publish the dashboard and noVNC on the tailnet only.**

   ```bash
   sudo tailscale serve --bg --tcp=8787 tcp://localhost:8787
   sudo tailscale serve --bg --tcp=6080 tcp://localhost:6080
   tailscale serve status
   ```

   Anyone on the tailnet can open both, and the dashboard has no login of its own, so keep the tailnet
   to Surya's devices. Without `tailscale serve`, an SSH tunnel works too:
   `ssh -L 8787:localhost:8787 -L 6080:localhost:6080 goat-vm`.

9. **VM: start in shadow mode.** In `.env`: `GOAT_MODE=shadow`, `LIVE_PORTALS=` (empty), and first
   `LANES_ENABLED=no_account` (Greenhouse, Ashby, Lever); later add `account` (Workday, Oracle).

   ```bash
   docker compose up -d
   docker compose ps                     # STATUS shows (healthy) after about two minutes
   curl -s localhost:8787/healthz
   docker compose logs -f goat
   ```

   After editing `.env`, `docker compose restart goat` applies it (goat reads `.env` at start).
   Chrome's own output is in `data/chrome.log`, noVNC's in `data/novnc.log`.

10. **VM: nightly backup at 03:15.** `crontab -e`, then add:

    ```bash
    15 3 * * * cd $HOME/autoapply && docker compose exec -T goat python scripts/backup_db.py >> data/backup.log 2>&1
    ```

    For an off-box copy, append `&& rclone copy data/backups <remote>:goat-backups` once rclone is set
    up on the VM for the bucket Surya picks (Neon Object Storage from the goat-dashboard project, or
    S3). To restore: `docker compose stop goat`, then
    `gunzip -c data/backups/goat-<stamp>.db.gz > data/goat.db && rm -f data/goat.db-wal data/goat.db-shm`,
    then `docker compose start goat`.

11. **Shadow trial: let 10-20 jobs run and compare with the PC's results.**

| Check | Pass when |
|---|---|
| Chrome under Xvfb | `docker compose exec goat python -m goat chrome` prints `"healthy": true`; screenshots in `records/` look like the PC's |
| Bitwarden | `docker compose exec goat python -c "from goat.creds import bitwarden as b; b.ensure_unlocked(); print(b.status())"` prints `unlocked`; a Workday sign-in fills from the Boeing item with no prompt |
| Gmail | an emailed code is entered (Oracle onsemi) and a verification link opens (Workday) |
| Telegram | a question arrives with buttons, the reply releases the job |
| CAPTCHA rate | `ready_for_you` for challenges is not much above the PC's rate |
| Restart | `docker compose restart goat` requeues the in-flight job, and it resumes |
| CAPTCHA handoff | the noVNC link in a `ready_for_you` ping opens the cloud browser on the phone, and the parked tab is there |
| Health | `docker compose ps` shows `(healthy)`; stopping Chrome turns it `(unhealthy)` until the watchdog relaunches it |

12. Once the trial passes, Surya reviews drafts as before. Portals go live only when he clears them.

**Updating** after a `git pull` on the VM: `docker compose build && docker compose up -d`.

## Notifications: Telegram or Gmail?

**Telegram is easier, and it is already built.** `goat/human/telegram_bot.py` sends each question with
the page screenshot and one-tap option buttons plus a "Hold it" button. It also takes free-text
replies, sends a ping when a job needs Surya, and posts a daily digest at 18:00. It uses long polling,
so it needs only outbound internet: it works the same on the PC and on a cloud VM. It is free, and
pushes reach the phone instantly. What is missing is configuration:

1. In Telegram, message **@BotFather**, send `/newbot`, pick a name, and copy the token.
2. Paste it in the dashboard's Settings tab (it is saved as `TELEGRAM_KEY`), send `/start` to the bot,
   and press **Find my chat ID**, which saves `TELEGRAM_OWNER_CHAT_ID` (the bot answers only that chat).
   **Send test message** checks the pair.
3. Restart goat and run `python -m goat digest`: the digest should arrive on the phone.

**Gmail would be new work.** goat's Gmail access is read-only (`gmail.readonly`), used for codes and
verification links. Sending notifications needs the `gmail.send` scope, a fresh OAuth consent, code to
compose messages, and code to read replies out of email threads to answer questions. Notifications
would also land among application emails and arrive only as fast as the mail app syncs. Gmail is
reasonable later as a backup channel for the daily digest, not as the main one.

The goat-dashboard web app (Next.js on Vercel, Neon) plans web push from an installable PWA. That can
replace or join Telegram once its phase 3 ships.
