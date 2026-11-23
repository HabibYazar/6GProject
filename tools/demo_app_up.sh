#!/bin/bash
# NSG demo bring-up, PLATFORM distro half (run as chancelor in Ubuntu). Verifies
# the engine is reachable across distros, then starts the backend and frontend if
# they are down. Idempotent — safe to run when things are already up.
ROOT=/home/chancelor/nsg_ai_platform
up() { [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null)" = "200" ]; }

echo "[3/4] Backend (uvicorn :8000)..."
for i in 1 2 3 4 5; do curl -s --max-time 4 http://localhost:8090/health >/dev/null 2>&1 && break; sleep 2; done
if curl -s --max-time 4 http://localhost:8090/health >/dev/null 2>&1; then
  echo "   engine reachable from platform distro"
else
  echo "   WARN: engine UNREACHABLE from platform distro (re-run, or: wsl -d Ubuntu-2004-nsoran -u root docker restart nsg-engine)"
fi
if ! curl -s --max-time 3 http://localhost:8000/health >/dev/null 2>&1; then
  # setsid: fully detach from THIS wsl.exe session — a plain nohup child is
  # killed when the session ends (the bat saw "UP", the user then saw
  # ERR_CONNECTION_REFUSED because both servers died with the session).
  (cd "$ROOT/nsg_ai" && setsid nohup ../venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 >/tmp/nsg_backend.log 2>&1 < /dev/null &)
fi
for i in $(seq 1 20); do curl -s --max-time 3 http://localhost:8000/health >/dev/null 2>&1 && break; sleep 1; done
printf '   backend: '; curl -s --max-time 4 http://localhost:8000/health || echo DOWN
echo

echo "[4/4] Frontend (:3000)..."
if ! up http://localhost:3000; then
  # setsid for the same session-teardown reason as the backend above.
  (cd "$ROOT/ui" && setsid nohup npm run dev >/tmp/nsg_frontend.log 2>&1 < /dev/null &)
fi
for i in $(seq 1 45); do up http://localhost:3000 && break; sleep 2; done
up http://localhost:3000 && echo "   frontend UP" || echo "   frontend DOWN - check: tail /tmp/nsg_frontend.log"
echo

# The bring-up above starts things; this PROVES they are reachable from where
# they are used — including the cross-distro engine path and 8091, neither of
# which any earlier check covered. --fix so a container missing a published port
# is repaired here rather than surfacing as a dead engine mid-demo.
"$ROOT/venv/bin/python" "$ROOT/tools/preflight.py" --fix
