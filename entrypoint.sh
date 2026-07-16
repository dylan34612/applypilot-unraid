#!/bin/sh
set -e

# Claude Code headless (-p) mode: skip the interactive first-run wizard
if [ ! -f "$HOME/.claude.json" ]; then
    echo '{"hasCompletedOnboarding": true}' > "$HOME/.claude.json"
fi

if [ "$1" = "idle" ]; then
    echo "ApplyPilot container is up. Open a console into it and run:"
    echo "  applypilot init                          # one-time setup wizard"
    echo "  applypilot doctor                        # verify everything is wired up"
    echo "  applypilot run all                       # discover > enrich > score > tailor > cover letters"
    echo "  applypilot apply --headless -m job-agent # auto-apply via the LiteLLM bridge"
    exec tail -f /dev/null
fi

exec "$@"
