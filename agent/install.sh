#!/usr/bin/env bash
# Install the Oracle Runner Agent to /opt/oracle-agent and run it as a systemd service.
# Usage: sudo bash install.sh
# Env overrides (optional):
#   ORACLE_URL   e.g. https://oracle-agent.femboy.us.ci
#   AGENT_NAME   e.g. runner-gh-01
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/opt/oracle-agent"

echo "[*] installing oracle-agent -> $DEST"
mkdir -p "$DEST"
cp "$SRC_DIR/agent.py" "$SRC_DIR/config.json" "$SRC_DIR/ws_client.py" "$SRC_DIR/mcp_proxy.py" "$DEST/"
cp -r "$SRC_DIR/handlers" "$DEST/"
chmod 644 "$DEST/agent.py" "$DEST/config.json" "$DEST/handlers"/*.py

# Apply env overrides to /opt/oracle-agent/config.json
if [ -n "${ORACLE_URL:-}" ] || [ -n "${AGENT_NAME:-}" ] || [ -n "${RUN_ID:-}" ]; then
  ORACLE_URL="${ORACLE_URL:-}" AGENT_NAME="${AGENT_NAME:-}" RUN_ID="${RUN_ID:-}" python3 - <<'PY'
import json, os
p = "/opt/oracle-agent/config.json"
c = json.load(open(p))
if os.environ.get("ORACLE_URL"):
    c["oracle_url"] = os.environ["ORACLE_URL"]
if os.environ.get("AGENT_NAME"):
    c["agent_name"] = os.environ["AGENT_NAME"]
if os.environ.get("RUN_ID"):
    try:
        c["run_id"] = int(os.environ["RUN_ID"])
    except ValueError:
        pass
json.dump(c, open(p, "w"), indent=2)
print("[*] config:", c.get("oracle_url"), c.get("agent_name"), "run_id:", c.get("run_id"))
PY
fi

echo "[*] installing systemd unit"
cp "$SRC_DIR/oracle-agent.service" /etc/systemd/system/oracle-agent.service
systemctl daemon-reload
systemctl enable oracle-agent
systemctl restart oracle-agent

echo "[*] done. status:"
systemctl --no-pager status oracle-agent | head -8