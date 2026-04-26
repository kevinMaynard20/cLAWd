#!/usr/bin/env bash
# Run the cLAWd stack: FastAPI backend (127.0.0.1:8000) + Next.js frontend
# (localhost:3000) in a single foreground command.
#
#   make dev           # via Makefile
#   bash scripts/dev.sh
#
# Ctrl-C stops both cleanly (cooperative SIGTERM, then SIGKILL on the dev
# ports as a belt-and-suspenders for any orphan worker thread). If either
# process dies on its own, the survivor is torn down too — no half-running
# stack.
#
# Re-running while a prior session is still bound to the dev ports is
# safe: the script kills any prior occupant before booting (idempotent).

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BACKEND_PORT="${LAWSCHOOL_BACKEND_PORT:-8000}"
FRONTEND_PORT="${LAWSCHOOL_FRONTEND_PORT:-3000}"

# ANSI colors only when stdout is a TTY.
if [[ -t 1 ]]; then
    YELLOW=$'\033[33m'; CYAN=$'\033[36m'; GREEN=$'\033[32m'
    RED=$'\033[31m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
    YELLOW=""; CYAN=""; GREEN=""; RED=""; DIM=""; BOLD=""; RESET=""
fi

log_api()  { while IFS= read -r line; do printf "%s[api]%s %s\n" "$CYAN"  "$RESET" "$line"; done; }
log_web()  { while IFS= read -r line; do printf "%s[web]%s %s\n" "$GREEN" "$RESET" "$line"; done; }

# ---------- kill helpers ----------

# Kill a process and its descendants. PID may belong to a subshell wrapping
# a pipeline; we walk the children explicitly because pipes don't form a
# proper process group on macOS by default.
kill_tree() {
    local pid="${1:-}"
    [[ -z "$pid" ]] && return 0
    # Children first (depth-first).
    local kids
    kids=$(pgrep -P "$pid" 2>/dev/null || true)
    for kid in $kids; do
        kill_tree "$kid"
    done
    kill -TERM "$pid" 2>/dev/null || true
}

# Anything still bound to a port → forcibly kill it. Last-resort cleanup.
kill_port() {
    local port="$1"
    local pids
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "${YELLOW}Killing existing :${port} occupant(s): $(echo "$pids" | tr '\n' ' ')${RESET}"
        # shellcheck disable=SC2086 — we want word-splitting here
        kill -TERM $pids 2>/dev/null || true
        sleep 0.3
        # shellcheck disable=SC2086
        kill -KILL $pids 2>/dev/null || true
    fi
}

cleanup() {
    local code=$?
    # Disarm the trap so a second Ctrl-C doesn't recurse.
    trap '' INT TERM EXIT

    echo
    echo "${YELLOW}${BOLD}Shutting down cLAWd stack…${RESET}"

    kill_tree "${BACKEND_PID:-}"
    kill_tree "${FRONTEND_PID:-}"

    # Give children half a second to exit cleanly, then nuke anything
    # still bound to the dev ports.
    sleep 0.5
    kill_port "$BACKEND_PORT"
    kill_port "$FRONTEND_PORT"

    # Reap any remaining background jobs we own (silently).
    wait 2>/dev/null || true

    echo "${YELLOW}All down.${RESET}"
    exit "$code"
}
trap cleanup INT TERM EXIT

# ---------- pre-flight ----------

if [[ ! -d "$REPO_ROOT/.venv" ]]; then
    echo "${RED}.venv not found at $REPO_ROOT/.venv${RESET}" >&2
    echo "${DIM}First-time setup: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'${RESET}" >&2
    exit 1
fi
if [[ ! -d "$REPO_ROOT/apps/web/node_modules" ]]; then
    echo "${RED}apps/web/node_modules not found${RESET}" >&2
    echo "${DIM}First-time setup: cd apps/web && npm install${RESET}" >&2
    exit 1
fi
if [[ ! -x "$REPO_ROOT/.venv/bin/uvicorn" ]]; then
    echo "${RED}uvicorn not in .venv${RESET}" >&2
    echo "${DIM}Run: .venv/bin/pip install -e '.[dev]'${RESET}" >&2
    exit 1
fi

# Idempotent restart: kill anything already on these ports.
kill_port "$BACKEND_PORT"
kill_port "$FRONTEND_PORT"

# ---------- banner ----------

echo "${BOLD}${CYAN}cLAWd dev stack${RESET}"
echo "${DIM}  backend:  http://127.0.0.1:${BACKEND_PORT}${RESET}"
echo "${DIM}  frontend: http://localhost:${FRONTEND_PORT}${RESET}"
echo "${DIM}  api docs: http://127.0.0.1:${BACKEND_PORT}/docs${RESET}"
echo "${DIM}  Ctrl-C to stop both.${RESET}"
echo

# ---------- start backend ----------
#
# Use process substitution `> >(log_api)` so `$!` resolves to the actual
# server PID, not the log-prefix co-process. A plain pipeline `cmd | log &`
# would set $! to the right-hand side, and `kill_tree` would walk the wrong
# tree leaving the real server orphaned on Ctrl-C. Process substitution is
# bash 3.2+ so this works on stock macOS.

PYTHONPATH="$REPO_ROOT/apps/api/src:$REPO_ROOT/apps/api" \
    "$REPO_ROOT/.venv/bin/uvicorn" main:app \
        --host 127.0.0.1 \
        --port "$BACKEND_PORT" \
        --reload \
        > >(log_api) 2>&1 &
BACKEND_PID=$!

# ---------- start frontend ----------

(
    cd "$REPO_ROOT/apps/web"
    # `npm run dev` execs into `next dev`. Pin port + host so a stale
    # `next.config.mjs` rewrite mismatch doesn't surprise us.
    exec npm run dev -- --hostname 127.0.0.1 --port "$FRONTEND_PORT"
) > >(log_web) 2>&1 &
FRONTEND_PID=$!

# ---------- watcher loop ----------
#
# `wait -n` would be tidier but we want to support macOS's stock bash 3.2,
# which doesn't have it. Poll instead. Either child dying triggers cleanup.

while true; do
    if ! kill -0 "$BACKEND_PID"  2>/dev/null; then
        echo "${RED}Backend exited unexpectedly.${RESET}"
        exit 1
    fi
    if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "${RED}Frontend exited unexpectedly.${RESET}"
        exit 1
    fi
    sleep 1
done
