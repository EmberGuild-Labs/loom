#!/usr/bin/env bash
#
# Install the Loom agent as a launchd service on macOS.
#
# Generates the plist from wherever this checkout actually lives, so there are
# no paths to hand-edit -- and so it works from a directory with spaces in the
# name, which a hand-written plist usually gets wrong.
#
#   ./install_macos.sh            install and start
#   ./install_macos.sh --uninstall  stop and remove
#
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.loom.agent"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PYTHON="$AGENT_DIR/venv/bin/python"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed ${LABEL}."
    exit 0
fi

# Fail early with a useful message rather than letting launchd fail silently in
# the background, which is the usual way a broken agent install goes unnoticed.
[[ -x "$PYTHON" ]] || {
    echo "No venv at $PYTHON" >&2
    echo "Run first:  cd '$AGENT_DIR' && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
    exit 1
}
[[ -f "$AGENT_DIR/config.yaml" ]] || {
    echo "No config.yaml in $AGENT_DIR" >&2
    echo "Run first:  cp '$AGENT_DIR/config.example.yaml' '$AGENT_DIR/config.yaml' and fill it in" >&2
    exit 1
}

# Refuse to install an agent that would just log auth errors forever.
if grep -q 'api_key: *"REPLACE_ME' "$AGENT_DIR/config.yaml"; then
    echo "config.yaml still has the placeholder api_key -- register this device on" >&2
    echo "the coordinator first, then paste its key in." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$AGENT_DIR/logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <!-- -u is not optional here. Python block-buffers stdout/stderr when they are
       files rather than a TTY, so without it the log stays empty for ages and a
       failing agent looks identical to a healthy silent one. -->
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>-u</string>
    <string>${AGENT_DIR}/loom_agent.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${AGENT_DIR}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <!-- Don't respawn faster than this if the agent is crash-looping. -->
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>${AGENT_DIR}/logs/loom-agent.log</string>

  <key>StandardErrorPath</key>
  <string>${AGENT_DIR}/logs/loom-agent.err.log</string>
</dict>
</plist>
PLIST_EOF

# bootout first so re-running this is a clean reinstall rather than an error.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "Installed ${LABEL}"
echo "  plist:  $PLIST"
echo "  logs:   $AGENT_DIR/logs/loom-agent.log"
echo
echo "Check it:   launchctl print gui/$(id -u)/${LABEL} | head -20"
echo "Follow log: tail -f '$AGENT_DIR/logs/loom-agent.log'"
