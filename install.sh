#!/bin/bash
# Install Argus — self-healing watchdog for Hermes Agent Gateway.
# Run this on the machine where Hermes gateway is running.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
INSTALL_DIR="$HERMES_HOME/argus"
DATA_DIR="$HERMES_HOME/watchdog"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SKILLS_DIR="$HERMES_HOME/hermes-agent/skills"

echo "=== Argus — Self-Healing Watchdog for Hermes ==="
echo ""

# Verify hermes exists
if [ ! -d "$HERMES_HOME/hermes-agent" ]; then
    echo "ERROR: Hermes not found at $HERMES_HOME/hermes-agent"
    echo "Set HERMES_HOME if your hermes is installed elsewhere."
    exit 1
fi

# 1. Create directories
mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$DATA_DIR/incidents" "$SYSTEMD_DIR"

# 2. Copy watchdog module
echo "[1/5] Installing watchdog module..."
cp -r argus/ "$INSTALL_DIR/argus/"

# 3. Install Hermes skill
echo "[2/5] Installing Hermes skill..."
if [ -d "$SKILLS_DIR" ]; then
    mkdir -p "$SKILLS_DIR/argus"
    cp skill/SKILL.md "$SKILLS_DIR/argus/SKILL.md"
    echo "  Skill installed to $SKILLS_DIR/argus/"
else
    echo "  Warning: Skills directory not found at $SKILLS_DIR"
    echo "  You can manually copy skill/SKILL.md to your skills directory."
fi

# 4. Install config if not exists
if [ ! -f "$DATA_DIR/config.yaml" ]; then
    echo "[3/5] Creating default config..."
    cp config.example.yaml "$DATA_DIR/config.yaml"
else
    echo "[3/5] Config already exists (keeping existing)"
fi

# 5. Check for PyYAML
if ! python3 -c "import yaml" 2>/dev/null; then
    echo "[4/5] Installing PyYAML..."
    pip3 install --user pyyaml 2>/dev/null \
        || pip install --user pyyaml 2>/dev/null \
        || pip3 install --user --break-system-packages pyyaml 2>/dev/null \
        || { echo "ERROR: Could not install pyyaml. Install manually: pip3 install pyyaml"; exit 1; }
else
    echo "[4/5] PyYAML already installed"
fi

# 6. Install systemd units
echo "[5/5] Installing systemd timer..."
cat > "$SYSTEMD_DIR/argus.service" << UNIT
[Unit]
Description=Argus Watchdog — Hermes Gateway Health Check
After=hermes-gateway.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -m argus -c "${DATA_DIR}/config.yaml"
WorkingDirectory="${INSTALL_DIR}"
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
TimeoutStartSec=120
UNIT

cp systemd/argus.timer "$SYSTEMD_DIR/argus.timer"
systemctl --user daemon-reload
systemctl --user enable --now argus.timer

echo ""
echo "=== Argus installed ==="
echo ""
echo "  Watchdog:  $INSTALL_DIR"
echo "  Data:      $DATA_DIR"
echo "  Config:    $DATA_DIR/config.yaml"
echo "  Skill:     $SKILLS_DIR/argus/SKILL.md"
echo "  Timer:     argus.timer (every 2 minutes)"
echo ""
echo "  Your agent now has the /argus skill."
echo "  Ask it: \"are you healthy?\" or \"any errors lately?\""
echo ""

# Run first probe
echo "Running initial health check..."
echo ""
cd "$INSTALL_DIR" && python3 -m argus --status
