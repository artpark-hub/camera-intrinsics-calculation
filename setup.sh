#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# MDCT-Proto (Mobile Dynamic Calibration Target) — Environment Setup
# ─────────────────────────────────────────────────────────────────────
# This script installs uv (if needed), creates a Python virtual
# environment, and installs all project dependencies.
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh
#
# After setup, run the calibration with:
#   uv run python main.py --rtsp rtsp://... --ipad --ipad_model ipad-pro-11
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_MIN="3.12"

# ── Colors ───────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'  # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 1. Install uv if not present ────────────────────────────────────
if command -v uv &>/dev/null; then
    ok "uv found: $(uv --version)"
else
    info "uv not found — installing..."
    if command -v curl &>/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget &>/dev/null; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "Neither curl nor wget found. Install uv manually: https://docs.astral.sh/uv/"
    fi

    # Source the uv env so it's available in this shell
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if command -v uv &>/dev/null; then
        ok "uv installed: $(uv --version)"
    else
        fail "uv installation failed. Install manually: https://docs.astral.sh/uv/"
    fi
fi

# ── 2. Create venv and sync dependencies ────────────────────────────
info "Syncing project dependencies (from pyproject.toml)..."
uv sync

ok "Dependencies installed."

# ── 3. macOS extras (optional) ──────────────────────────────────────
if [[ "$(uname -s)" == "Darwin" ]]; then
    info "macOS detected — installing optional Quartz dependency..."
    uv pip install 'pyobjc-framework-quartz>=12.1' 2>/dev/null && \
        ok "Quartz framework installed (enables local screen auto-detection)." || \
        warn "Quartz install failed — not critical. Use --ipad_model or --board_width_mm instead."
fi

# ── 4. Linux TTS check (optional) ───────────────────────────────────
if [[ "$(uname -s)" == "Linux" ]]; then
    if command -v espeak &>/dev/null || command -v spd-say &>/dev/null || command -v festival &>/dev/null; then
        ok "TTS engine found."
    else
        warn "No TTS engine found. Voice guidance will be disabled."
        warn "Install one with:  sudo apt install espeak  (or spd-say / festival)"
    fi
fi

# ── 5. Quick verification ───────────────────────────────────────────
info "Verifying imports..."
uv run python -c "
import cv2
import numpy as np
import flask
print(f'  opencv  : {cv2.__version__}')
print(f'  numpy   : {np.__version__}')
print(f'  flask   : {flask.__version__}')
print('  All imports OK.')
" || fail "Import verification failed. Check the error above."

# ── Done ─────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Quick start (iPad mode — recommended):"
echo ""
echo "    uv run python main.py --rtsp rtsp://YOUR_CAMERA_URL \\"
echo "        --ipad --ipad_model ipad-pro-11"
echo ""
echo "  Then open http://<your-ip>:8080/ on the iPad."
echo ""
echo "  See USER_GUIDE.md for full instructions."
echo "  See TECHNICAL_OVERVIEW.md for how it works."
echo ""
