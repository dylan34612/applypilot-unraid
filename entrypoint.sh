#!/bin/sh
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"

# ---------------------------------------------------------------------------
# Privileged prelude (root): give the pilot user access to the Docker socket,
# fix volume ownership, then re-exec this script as pilot via gosu. Every real
# process (uvicorn, Chrome, Claude Code) then runs unprivileged.
# ---------------------------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
    # Keep pilot's uid/gid in sync with the host (Unraid nobody:users = 99:100)
    usermod -o -u "$PUID" pilot 2>/dev/null || true
    groupmod -o -g "$PGID" users 2>/dev/null || true

    # Grant socket access without running anything as root: make a group whose
    # GID matches the socket's owner-group and add pilot to it.
    if [ -S /var/run/docker.sock ]; then
        SOCK_GID="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 0)"
        if [ "$SOCK_GID" != "0" ]; then
            if ! getent group "$SOCK_GID" >/dev/null 2>&1; then
                groupadd -g "$SOCK_GID" dockerhost 2>/dev/null || true
            fi
            GNAME="$(getent group "$SOCK_GID" | cut -d: -f1)"
            [ -n "$GNAME" ] && usermod -aG "$GNAME" pilot 2>/dev/null || true
        else
            # Socket owned by root group — relax perms so the group can use it
            chmod g+rw /var/run/docker.sock 2>/dev/null || true
        fi
    fi

    chown "$PUID:$PGID" /config /home/pilot 2>/dev/null || true
    exec gosu pilot "$0" "$@"
fi

# ---------------------------------------------------------------------------
# Unprivileged (pilot) from here on
# ---------------------------------------------------------------------------

# Claude Code headless (-p) mode: skip the interactive first-run wizard
if [ ! -f "$HOME/.claude.json" ]; then
    echo '{"hasCompletedOnboarding": true}' > "$HOME/.claude.json"
fi

# Start the virtual display + noVNC stack so headed Chrome is viewable in a
# browser (watch the agent, solve CAPTCHAs by hand). Best-effort.
start_display() {
    DISPLAY="${DISPLAY:-:99}"
    export DISPLAY
    RES="${VNC_RESOLUTION:-1280x900x24}"

    if command -v Xvfb >/dev/null 2>&1; then
        Xvfb "$DISPLAY" -screen 0 "$RES" -nolisten tcp &
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && break
            sleep 0.3
        done
        command -v fluxbox   >/dev/null 2>&1 && (fluxbox >/dev/null 2>&1 &)
        command -v x11vnc    >/dev/null 2>&1 && \
            x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 >/dev/null 2>&1 &
        if command -v websockify >/dev/null 2>&1; then
            WEBROOT=""
            for d in /usr/share/novnc /usr/share/webapps/novnc; do
                [ -d "$d" ] && WEBROOT="$d" && break
            done
            if [ -n "$WEBROOT" ]; then
                [ -f "$WEBROOT/vnc.html" ] && [ ! -e "$WEBROOT/index.html" ] && \
                    ln -sf vnc.html "$WEBROOT/index.html" 2>/dev/null || true
                websockify --web "$WEBROOT" "${NOVNC_PORT:-8485}" localhost:5900 >/dev/null 2>&1 &
                echo "noVNC on :${NOVNC_PORT:-8485} (view live browser)"
            fi
        fi
    fi
}

case "$1" in
    webui)
        start_display
        echo "ApplyPilot WebUI listening on :8484"
        exec uvicorn app:app --host 0.0.0.0 --port 8484 --app-dir /opt/webui
        ;;
    idle)
        start_display
        echo "ApplyPilot container is up (idle mode). Open a console and run:"
        echo "  applypilot init | doctor | run all | apply --headless"
        exec tail -f /dev/null
        ;;
    *)
        exec "$@"
        ;;
esac
