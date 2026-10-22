#!/usr/bin/env python3
"""Transcribe a wav file with local faster-whisper, then insert the text into
the currently focused window. This is the one-shot path: `dictate` uses it for
short takes, and as a fallback when the live streamer couldn't run.

English (and other latin text) is typed directly with ydotool, which leaves
the clipboard alone. Anything with non-ASCII characters (e.g. Urdu) is put on
the clipboard and pasted with Ctrl+V, because ydotool can only emit keycodes.

Transcription normally goes through model_server.py, which keeps the model
loaded (first use pays the ~7 s load, later uses do not). If the server can't
be started we fall back to loading the model in this process.

Server/insertion/history helpers live in vdlib.py, shared with streamer.py.
"""
import os
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vdlib import (  # noqa: E402
    append_history,
    ask_server,
    ensure_server,
    insert_text,
    run,
)

WAV = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dictate.wav"


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_audio(path: str) -> np.ndarray:
    """Decode to 16 kHz mono float32, which is what whisper expects.

    (faster_whisper's own decoder calls av.open(metadata_errors=...), which the
    installed PyAV no longer accepts, so we decode with ffmpeg ourselves.)
    """
    fd, tmp = tempfile.mkstemp(suffix=".f32", prefix="vd-")
    os.close(fd)
    try:
        r = run(
            ["ffmpeg", "-v", "error", "-y", "-i", path,
             "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", "16000", tmp]
        )
        if r.returncode != 0:
            die(f"ffmpeg failed: {r.stderr.decode(errors='replace')[:300]}")
        audio = np.fromfile(tmp, dtype=np.float32)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if audio.size < 1600:  # less than 0.1 s
        die("audio too short / empty")
    return audio


def transcribe_local(path: str) -> dict:
    t0 = time.time()
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(os.environ.get("SOTTO_MODEL", "small"), device="cpu", compute_type="int8", cpu_threads=8)
    except Exception as e:  # noqa: BLE001
        die(f"could not load whisper model: {e}")
    audio = load_audio(path)
    try:
        segments, info = model.transcribe(
            audio,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            language=None,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments if s.text.strip()).strip()
    except Exception as e:  # noqa: BLE001
        die(f"transcription failed: {e}")
    return {
        "status": "ok" if text else "no_speech",
        "text": text,
        "lang": info.language,
        "prob": round(info.language_probability, 2),
        "seconds": round(time.time() - t0, 1),
    }


t0 = time.time()
result = None
if ensure_server():
    result = ask_server(WAV)
if result is None:
    result = transcribe_local(WAV)

if result.get("status") == "error":
    die(result.get("error") or "server error")
if result.get("status") == "no_speech" or not result.get("text", "").strip():
    print("NO_SPEECH", file=sys.stderr)
    sys.exit(3)

text = result["text"].strip()
print(
    f"[lang={result.get('lang', '?')} p={result.get('prob', 0)} "
    f"server={result.get('seconds', '?')}s total={time.time() - t0:.1f}s] {text}",
    file=sys.stderr,
)

# Permanent record: lets you scroll back and check a long take after the fact.
append_history(result.get("lang", "?"), text)

insert_text(text)
print(text)
