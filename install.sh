#!/usr/bin/env bash
#===============================================================================
# Docflix Video Converter — Installer
#
# Usage:
#   ./install.sh              Install or update
#   ./install.sh --uninstall  Remove all installed files
#
# Installs to user-local directories (no sudo required):
#   ~/.local/share/docflix/        App files
#   ~/.local/share/icons/          App icon
#   ~/.local/share/applications/   .desktop launcher
#   ~/.local/bin/docflix           Terminal command
#===============================================================================

set -euo pipefail

#───────────────────────────────────────────────────────────────────────────────
# Config
#───────────────────────────────────────────────────────────────────────────────
APP_NAME="Docflix Video Converter"
APP_CMD="docflix"
INSTALL_DIR="$HOME/.local/share/docflix"
ICON_DIR="$HOME/.local/share/icons"
DESKTOP_DIR="$HOME/.local/share/applications"
BIN_DIR="$HOME/.local/bin"
DESKTOP_FILE="$DESKTOP_DIR/docflix.desktop"
ICON_FILE="$ICON_DIR/docflix.png"
BIN_FILE="$BIN_DIR/$APP_CMD"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Files to install (relative to SCRIPT_DIR)
APP_FILES=(
    "video_converter.py"
    "run_converter.sh"
    "logo.png"
)

#───────────────────────────────────────────────────────────────────────────────
# Helpers
#───────────────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()    { echo -e "${BLUE}  →${NC} $*"; }
success() { echo -e "${GREEN}  ✓${NC} $*"; }
warn()    { echo -e "${YELLOW}  ⚠${NC} $*"; }
error()   { echo -e "${RED}  ✗${NC} $*"; }
header()  { echo -e "\n${BLUE}$*${NC}"; }

#───────────────────────────────────────────────────────────────────────────────
# Uninstall
#───────────────────────────────────────────────────────────────────────────────
uninstall() {
    echo ""
    echo "========================================="
    echo "  🗑️  Uninstalling $APP_NAME"
    echo "========================================="
    echo ""

    local found=0

    if [[ -d "$INSTALL_DIR" ]]; then
        rm -rf "$INSTALL_DIR"
        success "Removed app directory: $INSTALL_DIR"
        found=1
    fi

    if [[ -f "$DESKTOP_FILE" ]]; then
        rm -f "$DESKTOP_FILE"
        success "Removed desktop entry: $DESKTOP_FILE"
        found=1
    fi

    if [[ -f "$ICON_FILE" ]]; then
        rm -f "$ICON_FILE"
        success "Removed icon: $ICON_FILE"
        found=1
    fi

    if [[ -f "$BIN_FILE" ]]; then
        rm -f "$BIN_FILE"
        success "Removed command: $BIN_FILE"
        found=1
    fi

    # Refresh desktop database
    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    fi

    echo ""
    if [[ $found -eq 1 ]]; then
        echo -e "${GREEN}  ✅ $APP_NAME has been uninstalled.${NC}"
    else
        warn "Nothing to uninstall — $APP_NAME does not appear to be installed."
    fi
    echo ""
    exit 0
}

#───────────────────────────────────────────────────────────────────────────────
# Main install
#───────────────────────────────────────────────────────────────────────────────

# Handle --uninstall flag
if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
fi

echo ""
echo "========================================="
echo "  🎬 $APP_NAME — Installer"
echo "========================================="
echo ""

#── 1. Check source files ──────────────────────────────────────────────────────
header "Checking source files..."
missing=0
for f in "${APP_FILES[@]}"; do
    if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
        error "Missing source file: $f"
        missing=1
    fi
done
if [[ $missing -eq 1 ]]; then
    echo ""
    error "One or more required files are missing. Please run install.sh from the project directory."
    exit 1
fi
success "All source files found"

#── 2. Check system dependencies ──────────────────────────────────────────────
header "Checking system dependencies..."

MISSING_PKGS=()

if ! command -v python3 &>/dev/null; then
    error "python3 not found"
    MISSING_PKGS+=("python3")
else
    PY_VER=$(python3 --version 2>&1)
    success "python3 found ($PY_VER)"
fi

if ! python3 -c "import tkinter" &>/dev/null; then
    error "python3-tk not found"
    MISSING_PKGS+=("python3-tk")
else
    success "tkinter found"
fi

if ! command -v ffmpeg &>/dev/null; then
    warn "ffmpeg not found — video conversion will not work"
    MISSING_PKGS+=("ffmpeg")
else
    FFMPEG_VER=$(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')
    success "ffmpeg found ($FFMPEG_VER)"
fi

if ! command -v pip3 &>/dev/null; then
    error "pip3 not found"
    MISSING_PKGS+=("python3-pip")
else
    success "pip3 found"
fi

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    echo ""
    warn "Missing system packages: ${MISSING_PKGS[*]}"
    echo ""
    echo "  Install them with:"
    echo "    sudo apt install ${MISSING_PKGS[*]}"
    echo ""
    read -rp "  Continue anyway? [y/N] " answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        echo "  Aborted."
        exit 1
    fi
fi

#── 3. Install Python packages ─────────────────────────────────────────────────
header "Installing Python packages..."

install_pip_pkg() {
    local pkg="$1"
    local import_name="${2:-$1}"
    if python3 -c "import $import_name" &>/dev/null; then
        success "$pkg already installed"
    else
        info "Installing $pkg..."
        if pip3 install --user "$pkg" --quiet; then
            success "$pkg installed"
        else
            warn "Failed to install $pkg — some features may not work"
        fi
    fi
}

install_pip_pkg "tkinterdnd2"
install_pip_pkg "Pillow" "PIL"

#── 4. Create install directories ─────────────────────────────────────────────
header "Creating directories..."

mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/logs"
mkdir -p "$ICON_DIR"
mkdir -p "$DESKTOP_DIR"
mkdir -p "$BIN_DIR"
success "Directories ready"

#── 5. Copy app files ──────────────────────────────────────────────────────────
header "Installing app files..."

for f in "${APP_FILES[@]}"; do
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
    success "Copied $f"
done

# Ensure scripts are executable
chmod +x "$INSTALL_DIR/run_converter.sh"
chmod +x "$INSTALL_DIR/video_converter.py"

# Generate logo_transparent.png from logo.png (removes white background)
if python3 -c "from PIL import Image" &>/dev/null; then
    info "Generating logo_transparent.png..."
    python3 -W ignore -c "
from PIL import Image
from pathlib import Path
install_dir = Path('$INSTALL_DIR')
src = install_dir / 'logo.png'
dst = install_dir / 'logo_transparent.png'
img = Image.open(src).convert('RGBA')
pixels = list(img.getdata())
new_pixels = [(r, g, b, 0) if r > 200 and g > 200 and b > 200 else (r, g, b, a) for r, g, b, a in pixels]
out = Image.new('RGBA', img.size)
out.putdata(new_pixels)
out.save(dst, 'PNG')
"
    if [[ -f "$INSTALL_DIR/logo_transparent.png" ]]; then
        success "logo_transparent.png generated"
    else
        warn "logo_transparent.png generation failed — app will use emoji fallback"
    fi
else
    warn "Pillow not available yet — logo_transparent.png will be generated on first run"
fi

#── 6. Install icon ────────────────────────────────────────────────────────────
header "Installing icon..."

cp "$SCRIPT_DIR/logo.png" "$ICON_FILE"
success "Icon installed: $ICON_FILE"

#── 7. Create .desktop file ────────────────────────────────────────────────────
header "Creating desktop launcher..."

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=Docflix Video Converter
Comment=Batch convert MKV videos to H.265/HEVC
Exec=bash $INSTALL_DIR/run_converter.sh
Path=$INSTALL_DIR
Terminal=false
Type=Application
Icon=$ICON_FILE
Categories=AudioVideo;Video;
Keywords=video;convert;hevc;h265;mkv;ffmpeg;
StartupNotify=false
EOF

chmod +x "$DESKTOP_FILE"
success "Desktop entry created: $DESKTOP_FILE"

# Refresh the desktop database so the app appears in the launcher
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    success "Desktop database updated"
fi

#── 8. Create terminal command ─────────────────────────────────────────────────
header "Creating terminal command..."

cat > "$BIN_FILE" <<EOF
#!/usr/bin/env bash
exec bash "$INSTALL_DIR/run_converter.sh" "\$@"
EOF

chmod +x "$BIN_FILE"
success "Terminal command created: $BIN_FILE"

#── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo "========================================="
echo -e "  ${GREEN}✅ Installation complete!${NC}"
echo "========================================="
echo ""
echo "  Launch options:"
echo "    • Search your app menu for \"Docflix Video Converter\""
echo "    • Or run from a terminal:  docflix"
echo ""
echo "  To uninstall:"
echo "    $SCRIPT_DIR/install.sh --uninstall"
echo "    (or from anywhere: bash $INSTALL_DIR/../docflix_uninstall.sh)"
echo ""
