#!/usr/bin/env bash
# A reproducible 30-second demo. Runs a fake MCP server behind the guard and
# prints what the model would see, then pages through the spill file.
#
#   bash examples/demo.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PYTHON="${PYTHON:-python3}"
SPILL_DIR="${SPILL_DIR:-$(mktemp -d)/spills}"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

call() {
  printf '%s\n' "$1" | $PYTHON -m mcp_output_guard run --max-chars 400 --spill-dir "$SPILL_DIR" -- \
    $PYTHON examples/demo_server.py
}

echo "### 1. the server returns a 37K-character log, the guard cuts it to 400"
CALL_JSON='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_logs"}}'
RAW="$(call "$CALL_JSON")"

SPILL="$($PYTHON - "$RAW" <<'PY'
import json, sys
message = json.loads(sys.argv[1])
print(message["result"]["_meta"]["output_guard"]["spill"])
PY
)"

$PYTHON - "$RAW" <<'PY'
import json, sys
message = json.loads(sys.argv[1])
result = message["result"]
for block in result["content"]:
    text = block["text"]
    print(text[:320] + ("..." if len(text) > 320 else ""))
print()
print("_meta:", json.dumps(result["_meta"], indent=2))
PY

echo
echo "### 2. the model pages through the spill file with output_guard_read"
READ_JSON=$($PYTHON - "$SPILL" <<'PY'
import json, sys
print(json.dumps({
    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
    "params": {"name": "output_guard_read", "arguments": {"spill": sys.argv[1], "offset": 0, "limit": 200}},
}))
PY
)
call "$READ_JSON" | $PYTHON -c '
import json, sys
message = json.load(sys.stdin)
print(message["result"]["content"][0]["text"][:400])
'

echo
echo "### 3. and searches it without loading the whole thing"
GREP_JSON=$($PYTHON - "$SPILL" <<'PY'
import json, sys
print(json.dumps({
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {"name": "output_guard_grep", "arguments": {"spill": sys.argv[1], "pattern": "job=.*399"}},
}))
PY
)
call "$GREP_JSON" | $PYTHON -c '
import json, sys
message = json.load(sys.stdin)
print(message["result"]["content"][0]["text"][:300])
'

echo
echo "spill directory: $SPILL_DIR"
