#!/usr/bin/env bash
# Launcher for Docflix Media Suite application

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/video_converter_$(date +%Y%m%d_%H%M%S).log"

echo "========================================="
echo "🎬 Docflix Media Suite"
echo "========================================="
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is required but not installed."
    exit 1
fi

# Check tkinter
if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "ERROR: tkinter is not installed."
    echo ""
    echo "Install it with:"
    echo "  Ubuntu/Debian: sudo apt install python3-tk"
    echo "  Fedora: sudo dnf install python3-tkinter"
    echo "  macOS: brew install python-tk"
    exit 1
fi

# Check ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "WARNING: ffmpeg is not installed."
    echo "Install it for video conversion:"
    echo "  Ubuntu/Debian: sudo apt install ffmpeg"
    echo "  Fedora: sudo dnf install ffmpeg"
    echo "  macOS: brew install ffmpeg"
    echo ""
fi

echo "Starting Docflix Media Suite..."
echo "Log file: $LOG_FILE"
echo ""

# Launch in background; stdout and stderr both go to the log file
# Uses the monolith directly (video_converter.py) for now.
# After full migration, this will change to:
#   python3 -m video_converter "$@"
nohup python3 "$SCRIPT_DIR/video_converter.py" "$@" >> "$LOG_FILE" 2>&1 &
APP_PID=$!

echo "Running in background (PID $APP_PID)"
echo "To follow the log:"
echo "  tail -f $LOG_FILE"
echo ""

# Keep only the 10 most recent log files
ls -t "$LOG_DIR"/video_converter_*.log 2>/dev/null | tail -n +11 | xargs -r rm --
