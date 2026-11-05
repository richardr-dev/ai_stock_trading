#!/usr/bin/env bash
# Daily swing routine: refresh prices + rates, scan swing setups. ~1 minute. Logs to logs/.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
COLUMNS=200 .venv/bin/python -m jarvis daily >> "logs/daily-$(date +%F).log" 2>&1
