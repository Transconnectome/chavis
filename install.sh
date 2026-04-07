#!/bin/bash
# Chavis Anti-Sycophancy System Installer
# Usage: bash install.sh

set -e

CLAUDE_DIR="${HOME}/.claude"

echo "=== Chavis Anti-Sycophancy System Installer ==="
echo ""

# Hooks
echo "[1/5] Installing hooks..."
cp hooks/chavis_prompt_classify.py "${CLAUDE_DIR}/hooks/"
cp hooks/chavis_stop_audit.py "${CLAUDE_DIR}/hooks/"
chmod +x "${CLAUDE_DIR}/hooks/chavis_prompt_classify.py"
chmod +x "${CLAUDE_DIR}/hooks/chavis_stop_audit.py"
echo "  ✅ hooks installed"

# Agent
echo "[2/5] Installing critic agent..."
cp agents/critic.md "${CLAUDE_DIR}/agents/"
echo "  ✅ critic agent installed"

# Commands
echo "[3/5] Installing commands..."
cp commands/challenge.md "${CLAUDE_DIR}/commands/"
cp commands/calibrate.md "${CLAUDE_DIR}/commands/"
echo "  ✅ /challenge and /calibrate installed"

# Skill
echo "[4/5] Installing chavis-antisyc skill..."
mkdir -p "${CLAUDE_DIR}/skills/chavis-antisyc"
cp skills/chavis-antisyc/SKILL.md "${CLAUDE_DIR}/skills/chavis-antisyc/"
echo "  ✅ chavis-antisyc skill installed"

# Tests
echo "[5/5] Installing test harness..."
mkdir -p "${CLAUDE_DIR}/tests/sycophancy/prompts" "${CLAUDE_DIR}/tests/sycophancy/results"
cp tests/sycophancy/conftest.py "${CLAUDE_DIR}/tests/sycophancy/"
cp tests/sycophancy/run_baseline.py "${CLAUDE_DIR}/tests/sycophancy/"
cp tests/sycophancy/prompts/*.json "${CLAUDE_DIR}/tests/sycophancy/prompts/"
echo "  ✅ test harness installed"

# Runtime directory
mkdir -p /tmp/chavis
echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Add hooks to ~/.claude/settings.json (see README.md)"
echo "  2. Add Anti-Sycophancy Protocol to RULES.md (see README.md)"
echo "  3. Restart Claude Code"
echo ""
echo "Usage:"
echo "  /challenge    — check last response for sycophancy"
echo "  /calibrate    — diagnose session sycophancy tendency"
