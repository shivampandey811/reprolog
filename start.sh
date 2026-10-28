#!/bin/bash
# Runs the whole reprolog project locally:
#   server   hosted dashboard (Cloudflare Worker + local D1)   http://localhost:8787
#   demo     a small app with reprolog, sending to the server  http://localhost:8001
#   viewer   the library's local viewer for the demo app       http://127.0.0.1:7999
# Ctrl+C stops everything. First run installs what's missing (.venv, server/node_modules).

ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVER_PORT=${SERVER_PORT:-8787}
DEMO_PORT=${DEMO_PORT:-8001}
VIEWER_PORT=${VIEWER_PORT:-7999}
STATE="$ROOT/.reprolog"   # ignored by git; holds the demo's SQLite file and dev token

[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

pids=()
cleanup() {
  echo; echo "Stopping server, demo and viewer..."
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null; done
  pkill -P $$ 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap cleanup INT TERM HUP

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
for p in $SERVER_PORT $DEMO_PORT $VIEWER_PORT; do
  if port_busy "$p"; then
    echo "Port $p is already in use. Stop whatever is running there (lsof -iTCP:$p) and try again."
    exit 1
  fi
done

prefix() { while IFS= read -r line; do printf '\033[%sm%-7s\033[0m %s\n' "$1" "$2" "$line"; done; }

# --- setup ------------------------------------------------------------------------
mkdir -p "$STATE"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "Creating .venv for the demo app..."
  python3 -m venv "$ROOT/.venv" && "$ROOT/.venv/bin/pip" install -q uvicorn starlette requests httpx || exit 1
fi
cd "$ROOT/server" || exit 1
[ -d node_modules ] || { echo "Installing server dependencies..."; npm install --silent || exit 1; }
if [ ! -f .dev.vars ]; then
  printf 'SESSION_SECRET=%s\nDEV_LOGIN=1\n' "$(openssl rand -hex 24)" > .dev.vars
fi
npx wrangler d1 execute reprolog --local --file schema.sql >/dev/null 2>&1 || { echo "Could not set up the local database"; exit 1; }

# --- server (local only; never touches the real D1 database) ---------------------
( npx wrangler dev --port $SERVER_PORT --test-scheduled 2>&1 | prefix "36" "server" ) &
pids+=($!)
for i in $(seq 1 60); do curl -s -o /dev/null "http://localhost:$SERVER_PORT/" && break; sleep 1; done

# A write token for the demo, created through the dev login and reused on later runs.
TOKEN_FILE="$STATE/dev-token"
if [ ! -s "$TOKEN_FILE" ]; then
  JAR="$(mktemp)"
  curl -s -c "$JAR" -o /dev/null "http://localhost:$SERVER_PORT/auth/dev"
  curl -s -b "$JAR" -H "X-Reprolog: 1" -X POST -d '{"name":"start.sh demo","scope":"write"}' \
    "http://localhost:$SERVER_PORT/api/tokens" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' > "$TOKEN_FILE"
  rm -f "$JAR"
fi

# --- demo app + local viewer -----------------------------------------------------
cd "$ROOT" || exit 1
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" PYTHONUNBUFFERED=1
export REPROLOG_ENABLED=1 REPROLOG_CAPTURE_ALL=1 REPROLOG_DB="$STATE/demo.db"
export REPROLOG_TOKEN="$(cat "$TOKEN_FILE")" REPROLOG_PROJECT=dev REPROLOG_ENDPOINT="http://localhost:$SERVER_PORT"
( cd tests && "$ROOT/.venv/bin/python" -m uvicorn demo_app:app --port $DEMO_PORT 2>&1 | prefix "34" "demo" ) &
pids+=($!)
( cd tests && "$ROOT/.venv/bin/python" -m reprolog --db "$REPROLOG_DB" ui --app demo_app:app --port $VIEWER_PORT --no-browser 2>&1 | prefix "35" "viewer" ) &
pids+=($!)

cat <<MSG

  dashboard  http://localhost:$SERVER_PORT/app   (click "Local dev login")
  website    http://localhost:$SERVER_PORT/
  demo app   http://localhost:$DEMO_PORT/ok     (every request shows up in project "dev")
  viewer     http://127.0.0.1:$VIEWER_PORT/app   (the library's local viewer)

  Make some traffic:  curl localhost:$DEMO_PORT/ok
                      curl localhost:$DEMO_PORT/handled                  (logged exception, 500)
                      curl -X POST localhost:$DEMO_PORT/api/order -d "\$(printf '{"sku":"empty"}' | base64)"
                      (the demo expects base64 bodies; this one fails with an IndexError)
  Ctrl+C stops everything.

MSG

[ -n "$NO_OPEN" ] || ( sleep 3; open "http://localhost:$SERVER_PORT/auth/dev" ) &
wait
