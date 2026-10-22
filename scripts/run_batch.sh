#!/usr/bin/env bash
# Scheduled refresh of the Salesforce docs index. Example crontab (Sundays 2am):
#   0 2 * * 0 /abs/path/sfdocs-rag/scripts/run_batch.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOCK="$ROOT/data/.run.lock"
mkdir -p "$ROOT/data"
# one run at a time
exec 9>"$LOCK"
if command -v flock >/dev/null; then flock -n 9 || { echo "already running"; exit 0; }; fi
"$ROOT/.venv/bin/sfdocs" --config "$ROOT/config.yaml" run-all >> "$ROOT/data/batch-$(date +%Y%m%d).json" 2>>"$ROOT/data/batch-errors.log"
