#!/usr/bin/env bash
# Start the JARVIS dashboard on 127.0.0.1:8501 (laptop only) and publish it to your tailnet
# with `tailscale serve` (HTTPS, tailnet-only — NOT public; that would be `funnel`).
set -uo pipefail
cd "$(dirname "$0")/.."
PORT=${PORT:-8501}
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale

# make sure the local AI is up
if ! curl -s -m 2 http://localhost:11434/api/tags >/dev/null; then
  (nohup ollama serve >/tmp/jarvis-ollama.log 2>&1 &)
fi

publish() {
  for i in {1..30}; do curl -s -m 1 "http://127.0.0.1:$PORT" >/dev/null && break; sleep 1; done
  if "$TS" status >/dev/null 2>&1; then
    "$TS" serve --bg "$PORT" >/dev/null 2>&1 && \
      echo "Tailnet URL: https://$("$TS" status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')/"
  else
    echo "Tailscale not running — dashboard is local only: http://127.0.0.1:$PORT"
  fi
}
publish &

exec .venv/bin/streamlit run app/dashboard.py \
  --server.address 127.0.0.1 --server.port "$PORT" --server.headless true \
  --browser.gatherUsageStats false --theme.base dark
