#!/usr/bin/env bash
# Container entrypoint for the cloud VM (docs/CLOUD_DEPLOY.md). Starts a virtual display for Chrome
# (Xvfb), a VNC server on it (x11vnc, container-local only) and noVNC, so Surya can click a CAPTCHA in
# the real cloud browser from his phone. Then runs the command (default: python -m goat run); goat
# launches Chrome itself on $CHROME_DISPLAY. If any piece exits, the container exits and Docker's
# restart policy brings the whole set back (in-flight jobs are requeued on start).
set -euo pipefail

DISPLAY_NUM="${CHROME_DISPLAY:-:99}"
SCREEN="${XVFB_SCREEN:-1440x900x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
NOVNC_WEB="${NOVNC_WEB:-/usr/share/novnc}"
export DISPLAY="$DISPLAY_NUM"

cd /app
mkdir -p data records proposals chrome-profile "${BITWARDENCLI_APPDATA_DIR:-/app/secrets/bitwarden-cli}"

[ -f /app/.env ] || echo "entrypoint: /app/.env is missing; goat runs on defaults (copy .env next to docker-compose.yml)" >&2

# A killed container leaves the X lock and Chrome's profile lock behind. Nothing else uses this
# display or profile inside the container, so both are stale here.
rm -f "/tmp/.X${DISPLAY_NUM#:}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM#:}"
rm -f chrome-profile/SingletonLock chrome-profile/SingletonSocket chrome-profile/SingletonCookie

Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp &
xvfb=$!
for _ in $(seq 1 50); do
  [ -S "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ] && break
  sleep 0.2
done
[ -S "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ] || { echo "entrypoint: Xvfb did not start" >&2; exit 1; }

# x11vnc listens on the container's loopback only; websockify (noVNC) is the one door in, and
# docker-compose publishes that on the VM's loopback only, for Tailscale to carry.
vnc_auth=(-nopw)
if [ -n "${VNC_PASSWORD:-}" ]; then
  umask 077
  printf '%s\n' "$VNC_PASSWORD" > /tmp/vncpass
  vnc_auth=(-passwdfile /tmp/vncpass)
  umask 022
fi
x11vnc -display "$DISPLAY_NUM" -rfbport "$VNC_PORT" -localhost -forever -shared -noxdamage -quiet \
  "${vnc_auth[@]}" -o /app/data/x11vnc.log &
vnc=$!

websockify --web "$NOVNC_WEB" "0.0.0.0:${NOVNC_PORT}" "localhost:${VNC_PORT}" >> /app/data/novnc.log 2>&1 &
novnc=$!

if [ "$#" -eq 0 ]; then
  set -- python -m goat run
fi
"$@" &
main=$!

stop() {
  kill -TERM "$main" 2>/dev/null || true
  wait "$main" 2>/dev/null || true
  kill -TERM "$novnc" "$vnc" "$xvfb" 2>/dev/null || true
  exit 143
}
trap stop TERM INT

# Whichever exits first decides: the main command's own exit code, or a display piece dying.
set +e
wait -n "$main" "$xvfb" "$vnc" "$novnc"
code=$?
set -e
if kill -0 "$main" 2>/dev/null; then
  echo "entrypoint: a display process exited (code $code); stopping goat so the container restarts" >&2
  kill -TERM "$main" 2>/dev/null || true
  wait "$main" 2>/dev/null || true
  code=1
fi
kill -TERM "$novnc" "$vnc" "$xvfb" 2>/dev/null || true
exit "$code"
