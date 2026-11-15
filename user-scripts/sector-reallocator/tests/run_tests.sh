#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONDONTWRITEBYTECODE=1
bash -n sector_reallocator.sh
python3 -B - <<'PY'
import ast
from pathlib import Path
for path in (Path('sata_reallocator.py'), Path('unraid_launcher.py')):
    compile(ast.parse(path.read_text()), str(path), 'exec')
print('Python syntax: PASS')
PY
python3 -B tests/check_engine.py
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
test_log=$(mktemp)
trap 'rm -f -- "$test_log"' EXIT
if bash sector_reallocator.sh > "$test_log" 2>&1; then
    echo 'Unconfigured launcher unexpectedly succeeded.' >&2
    exit 1
fi
if ! grep -q 'Set DEVICE, EXPECTED_SERIAL' "$test_log"; then
    cat "$test_log" >&2
    exit 1
fi
