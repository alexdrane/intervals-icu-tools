#!/usr/bin/env bash
# Sets up the training dashboard: deps, config, launcher, icon, optional autostart.
# Usage: ./install.sh [--autostart]
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

echo "== System packages (needs sudo) =="
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
    python3-pip libportaudio2

echo "== Python packages =="
pip install --user --break-system-packages -r "$REPO/requirements.txt"

echo "== Config =="
CFG="$HOME/.config/intervals-icu"
mkdir -p "$CFG" "$HOME/.cache/training-brief" "$HOME/.local/share/training-brief"
for f in config training-plan benchmarks; do
    [ -e "$CFG/$f.json" ] || cp "$REPO/$f.example.json" "$CFG/$f.json"
done
echo "Edit $CFG/config.json: set athlete_id and api_key (intervals.icu -> Settings -> Developer Settings)."

echo "== Launcher =="
mkdir -p "$HOME/bin" "$HOME/.local/share/icons" "$HOME/.local/share/applications"
for t in icu food training-brief.py; do ln -sf "$REPO/$t" "$HOME/bin/$t"; done
cp "$REPO/assets/training-brief.svg" "$HOME/.local/share/icons/"
sed -e "s|@REPO@|$REPO|" -e "s|@ICON@|$HOME/.local/share/icons/training-brief.svg|" \
    "$REPO/training-brief.desktop.in" > "$HOME/.local/share/applications/training-brief.desktop"
if [ "${1:-}" = "--autostart" ]; then
    mkdir -p "$HOME/.config/autostart"
    cp "$HOME/.local/share/applications/training-brief.desktop" "$HOME/.config/autostart/"
fi

echo "== Local model (coach tips, food logging) =="
if command -v ollama >/dev/null; then
    ollama pull qwen2.5:7b && ollama pull qwen2.5:3b
else
    echo "Ollama not found. Install from https://ollama.com, then: ollama pull qwen2.5:7b && ollama pull qwen2.5:3b"
    echo "(The dashboard runs without it; coach tips and food logging need it.)"
fi
echo "Done. Launch 'Training Dashboard' from your app menu, or: python3 $REPO/training-brief.py"
