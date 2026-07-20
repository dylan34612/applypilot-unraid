#!/bin/sh
set -e

# Claude Code headless (-p) mode: skip the interactive first-run wizard
if [ ! -f "$HOME/.claude.json" ]; then
    echo '{"hasCompletedOnboarding": true}' > "$HOME/.claude.json"
fi

# Start the virtual display + noVNC stack so headed Chrome is viewable in a
# browser (watch the agent, solve CAPTCHAs by hand). Best-effort: if any
# piece is missing the WebUI/CLI still work headless.
start_display() {
    DISPLAY="${DISPLAY:-:99}"
    export DISPLAY
    RES="${VNC_RESOLUTION:-1280x900x24}"

    if command -v Xvfb >/dev/null 2>&1; then
        Xvfb "$DISPLAY" -screen 0 "$RES" -nolisten tcp &
        # Wait for the display socket before starting clients
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && break
            sleep 0.3
        done
        command -v fluxbox   >/dev/null 2>&1 && (fluxbox >/dev/null 2>&1 &)
        command -v x11vnc    >/dev/null 2>&1 && \
            x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 >/dev/null 2>&1 &
        # noVNC (websockify) bridges VNC -> browser on NOVNC_PORT
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
