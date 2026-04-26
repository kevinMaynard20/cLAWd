#!/usr/bin/env bash
# Hard-kill anything bound to the cLAWd dev ports. Use this when `make dev`
# crashed without cleaning up, or if a prior session was force-killed
# (SIGKILL bypasses the trap in dev.sh).
#
#   make stop
#   bash scripts/stop.sh
#
# Idempotent — safe to run when nothing is bound.

set -u

BACKEND_PORT="${LAWSCHOOL_BACKEND_PORT:-8000}"
FRONTEND_PORT="${LAWSCHOOL_FRONTEND_PORT:-3000}"

if [[ -t 1 ]]; then
    YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
    YELLOW=""; DIM=""; RESET=""
fi

kill_port() {
    local port="$1"
    local pids
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    if [[ -z "$pids" ]]; then
        echo "${DIM}:${port} clear.${RESET}"
        return 0
    fi
    echo "${YELLOW}Killing :${port} occupant(s): $(echo "$pids" | tr '\n' ' ')${RESET}"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
    sleep 0.4
    # Re-check; if anything still there, SIGKILL it.
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill -KILL $pids 2>/dev/null || true
    fi
}

kill_port "$BACKEND_PORT"
kill_port "$FRONTEND_PORT"

# Also nuke any stray uvicorn/next processes we own that aren't bound to
# a port (e.g., killed mid-startup before they got around to listening).
# We match by command name to avoid touching unrelated python/node.
for pat in "uvicorn main:app" "next dev"; do
    matched=$(pgrep -fl "$pat" 2>/dev/null | awk '{print $1}' || true)
    if [[ -n "$matched" ]]; then
        echo "${YELLOW}Killing stray ${pat}: $(echo "$matched" | tr '\n' ' ')${RESET}"
        # shellcheck disable=SC2086
        kill -KILL $matched 2>/dev/null || true
    fi
done

echo "${DIM}Done.${RESET}"
