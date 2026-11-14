#!/bin/bash
# Restore state.json from the most recent restockr-state artifact (same repo).
set -euo pipefail
if [ -f state.json ]; then
  echo "State already present"
  exit 0
fi
ID="$(gh api "repos/${GITHUB_REPOSITORY}/actions/artifacts?name=restockr-state&per_page=5" \
  --jq '[.artifacts[] | select(.expired==false)] | sort_by(.created_at) | reverse | .[0].id' || true)"
if [ -z "${ID:-}" ] || [ "${ID}" = "null" ]; then
  echo "No previous state artifact found"
  exit 0
fi
gh api "repos/${GITHUB_REPOSITORY}/actions/artifacts/${ID}/zip" > /tmp/restockr-state.zip
python3 -c "
import zipfile
from pathlib import Path
z = zipfile.ZipFile('/tmp/restockr-state.zip')
for n in z.namelist():
    if n.endswith('state.json'):
        Path('state.json').write_bytes(z.read(n))
        print('Restored', n)
        break
else:
    raise SystemExit('state.json not in artifact')
"
