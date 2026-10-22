#!/usr/bin/env bash
# Install WhisperQuill into ~/.local/share/whisperquill and register the hotkey.
# Everything user-level. The three root steps for /dev/uinput are printed at
# the end and left for you to run.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/whisperquill"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

say() { printf '%s\n' "$*"; }

say "== checking dependencies"
missing=0
for b in python3 ffmpeg ydotool wl-copy pw-play notify-send; do
    if command -v "$b" >/dev/null 2>&1; then
        say "   ok      $b"
    else
        say "   MISSING $b"
        missing=1
    fi
done
if [ "$missing" = 1 ]; then
    say ""
    say "Install the missing ones first, e.g. on Arch:"
    say "   sudo pacman -S python ffmpeg ydotool wl-clipboard pipewire libnotify"
    say "   (python3 is already there if you are reading this, but venv needs it)"
    exit 1
fi

say "== copying files to $DEST"
mkdir -p "$DEST" "$APPS"
cp "$SRC"/dictate "$SRC"/streamer.py "$SRC"/model_server.py "$SRC"/transcribe.py "$SRC"/vdlib.py "$DEST"/
mkdir -p "$DEST/sounds"
cp "$SRC"/sounds/*.wav "$DEST/sounds"/
chmod +x "$DEST/dictate"

if [ -x "$DEST/.venv/bin/python" ]; then
    say "== reusing existing venv"
else
    say "== creating python venv (this downloads faster-whisper, a few hundred MB)"
    python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install --quiet --upgrade pip
"$DEST/.venv/bin/pip" install --quiet --upgrade faster-whisper

say "== registering the global shortcut (Ctrl+Alt+Space)"
sed "s|__EXEC__|$DEST/dictate|" "$SRC/packaging/whisperquill.desktop" \
    > "$APPS/whisperquill.desktop"
update-desktop-database "$APPS" >/dev/null 2>&1 || true

say ""
say "== almost done. two root steps remain:"
say ""
say "   sudo modprobe uinput"
say "   echo uinput | sudo tee /etc/modules-load.d/uinput.conf"
say ""
say "   sudo install -m 644 '$SRC/packaging/ydotoold@.service' /etc/systemd/system/"
say "   sudo systemctl enable --now ydotoold@$(id -u)"
say ""
say "Then: put your cursor in a text field and press Ctrl+Alt+Space."
say "If the shortcut does nothing, add it once in"
say "System Settings -> Keyboard -> Shortcuts -> Custom Shortcuts -> WhisperQuill."
