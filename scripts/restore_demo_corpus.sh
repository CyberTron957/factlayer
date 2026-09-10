#!/bin/bash
# Restore the pre-loaded India-macroeconomy demo corpus.
# Use after "Clear entire corpus" wipes the demo, or after experimenting:
#   ./scripts/restore_demo_corpus.sh
# Refuses to run while a processing job is active on the demo DB.
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP="data_macro_backup"
DB="data_macro"

if [ ! -f "$BACKUP/knowledge.db" ]; then
  echo "no backup found in $BACKUP/ — nothing to restore" >&2
  exit 1
fi

if [ -f "$DB/jobs.json" ] && python3 -c "
import json,sys
jobs=json.load(open('$DB/jobs.json'))
sys.exit(0 if any(j.get('status') in ('queued','running') for j in jobs.values()) else 1)
" 2>/dev/null; then
  echo "a job is running on $DB — cancel it before restoring" >&2
  exit 1
fi

cp "$BACKUP/knowledge.db" "$DB/knowledge.db"
mkdir -p "$DB/crops"
cp "$BACKUP"/crops/*.png "$DB/crops/" 2>/dev/null || true
echo "restored demo corpus: $(sqlite3 "$DB/knowledge.db" 'SELECT COUNT(*) FROM documents') docs, $(sqlite3 "$DB/knowledge.db" 'SELECT COUNT(*) FROM facts') facts, $(sqlite3 "$DB/knowledge.db" 'SELECT COUNT(*) FROM relations') relations"
