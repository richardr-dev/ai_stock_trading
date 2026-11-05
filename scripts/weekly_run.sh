#!/usr/bin/env bash
# Weekly job: ingest -> DCF/signals/portfolio -> AI brief. Logs to logs/.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
curl -s -m 2 http://localhost:11434/api/tags >/dev/null || { (nohup ollama serve >/tmp/jarvis-ollama.log 2>&1 &); sleep 5; }
COLUMNS=200 .venv/bin/python -m jarvis run >> "logs/run-$(date +%F).log" 2>&1
