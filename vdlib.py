"""Shared helpers for the whisperquill scripts (transcribe.py, streamer.py).

Keeps the model-server client, the text-insertion logic and the history log
in one place so the live and the one-shot paths cannot drift apart.
"""
import json
import os
import socket
import subprocess
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
SOCK = os.path.join(STATE, "model.sock")
SERVER = os.path.join(BASE, "model_server.py")
SERVER_LOG = os.path.join(BASE, "server.log")
HISTORY = os.path.join(BASE, "history.log")

# Ctrl+V as raw keycodes (layout independent): KEY_LEFTCTRL=29, KEY_V=47
PASTE_KEYS = ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]


def run(cmd, **kw):
    return subprocess.run(cmd, check=False, capture_output=True, **kw)


def ensure_server(timeout: float = 90.0) -> bool:
    """Start the model server on demand if its socket isn't there."""
    if os.path.exists(SOCK):
        return True
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(SERVER_LOG, "a") as logfh:
            subprocess.Popen(
                [os.path.join(BASE, ".venv/bin/python"), SERVER],
                stdout=logfh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception as e:  # noqa: BLE001
        print(f"[server spawn failed: {e}]", flush=True)
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(SOCK):
            return True
        time.sleep(0.25)
    return os.path.exists(SOCK)


def ask_server(path, *, language=None, prompt=None, beam_size=None):
    """Ask the model server to transcribe `path`.

    Returns the server's dict, or None if the server couldn't be reached.
    Older/plain requests (just a path) are still accepted by the server.
    """
    req = {"path": path}
    # WHISPERQUILL_LANGUAGE pins a language (e.g. "en", "ur"); unset = auto-detect
    language = language or os.environ.get("WHISPERQUILL_LANGUAGE") or None
    if language:
        req["language"] = language
    if prompt:
        req["prompt"] = prompt
    if beam_size:
        req["beam_size"] = beam_size
    payload = (path + "\n") if set(req) == {"path"} else (json.dumps(req) + "\n")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(900)
        s.connect(SOCK)
        s.sendall(payload.encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        if not data.strip():
            return None
        return json.loads(data.decode())
    except Exception as e:  # noqa: BLE001
        print(f"[server unavailable: {e}]", flush=True)
        return None


def insert_text(text: str) -> None:
    """Put `text` where the cursor is.

    ASCII is typed directly with ydotool (clipboard untouched). Anything with
    non-ASCII characters - Urdu, Arabic, Chinese - is put on the clipboard and
    pasted with Ctrl+V, because ydotool can only emit physical key codes.
    """
    if text.isascii():
        r = run(["ydotool", "type", "--file", "-"], input=text.encode())
        if r.returncode != 0:
            raise RuntimeError("ydotool type: " + r.stderr.decode(errors="replace")[:200])
    else:
        r = run(["wl-copy", "--"], input=text.encode())
        if r.returncode != 0:
            raise RuntimeError("wl-copy: " + r.stderr.decode(errors="replace")[:200])
        time.sleep(0.15)
        r = run(PASTE_KEYS)
        if r.returncode != 0:
            raise RuntimeError("ydotool key: " + r.stderr.decode(errors="replace")[:200])


def append_history(lang: str, text: str) -> None:
    """timestamp, detected language, text - so a long session can be audited."""
    try:
        with open(HISTORY, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{lang}\t{text}\n")
    except OSError as e:
        print(f"[history.log: {e}]", flush=True)
