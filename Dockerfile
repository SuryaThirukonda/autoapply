# goat for the cloud VM (docs/CLOUD_DEPLOY.md): Python 3.12 + requirements.lock, Google Chrome stable on
# amd64 (Debian's Chromium on arm64: Google ships no Linux ARM build), Xvfb + x11vnc + noVNC for the
# CAPTCHA handoff, and the Bitwarden CLI on Node 22. No personal file goes into the image (.dockerignore):
# config, résumés, secrets and runtime data are bind mounts (docker-compose.yml).

FROM node:22-bookworm-slim AS bw
ARG BW_CLI_VERSION=2026.9.1
RUN npm install -g --omit=dev "@bitwarden/cli@${BW_CLI_VERSION}" && npm cache clean --force

FROM python:3.12-slim-bookworm
ARG TARGETARCH
ARG UID=1000
ARG GID=1000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 TZ=America/Chicago \
    CHROME_DISPLAY=:99 BITWARDENCLI_APPDATA_DIR=/app/secrets/bitwarden-cli

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg tini locales tzdata procps \
      xvfb x11vnc novnc websockify \
      fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
 && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen \
 && if [ "$TARGETARCH" = "arm64" ]; then \
      apt-get install -y --no-install-recommends chromium; \
    else \
      curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
      && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
           > /etc/apt/sources.list.d/google-chrome.list \
      && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# Bitwarden CLI: Node and the package from the first stage (same Debian release, same glibc).
COPY --from=bw /usr/local/bin/node /usr/local/bin/node
COPY --from=bw /usr/local/lib/node_modules/@bitwarden /usr/local/lib/node_modules/@bitwarden
RUN printf '#!/bin/sh\nexec /usr/local/bin/node /usr/local/lib/node_modules/@bitwarden/cli/build/bw.js "$@"\n' > /usr/local/bin/bw \
 && chmod +x /usr/local/bin/bw && bw --version

WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock

RUN groupadd -g "${GID}" goat && useradd -m -u "${UID}" -g "${GID}" -s /bin/bash goat
COPY --chown=goat:goat . .
# A Windows checkout may carry CRLF line endings; bash refuses them.
RUN sed -i 's/\r$//' scripts/entrypoint.sh && chmod +x scripts/entrypoint.sh \
 && mkdir -p data records proposals chrome-profile secrets resumes \
 && chown -R goat:goat /app

USER goat
# Documentation only: docker-compose.yml publishes both on the VM's loopback, never publicly.
EXPOSE 8787 6080
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('DASHBOARD_PORT', '8787'), timeout=8).status == 200 else 1)"
ENTRYPOINT ["tini", "--", "/app/scripts/entrypoint.sh"]
CMD ["python", "-m", "goat", "run"]
