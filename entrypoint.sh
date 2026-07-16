#!/bin/sh
set -e

# Claude Code headless (-p) mode: skip the interactive first-run wizard
if [ ! -f "$HOME/.claude.json" ]; then
    echo '{"hasCompletedOnboarding": true}' > "$HOME/.claude.json"
fi

case "$1" in
    webui)
        echo "ApplyPilot WebUI listening on :8484"
        exec uvicorn app:app --host 0.0.0.0 --port 8484 --app-dir /opt/webui
        ;;
    idle)
        echo "ApplyPilot container is up (idle mode). Open a console and run:"
        echo "  applypilot init | doctor | run all | apply --headless"
        exec tail -f /dev/null
        ;;
    *)
        exec "$@"
        ;;
esac
